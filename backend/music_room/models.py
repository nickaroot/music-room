from __future__ import annotations

import uuid
import subprocess
from io import FileIO
from typing import List, Union
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.fields.files import FieldFile
from django.db.models.manager import Manager
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from django_app.settings import AWS_S3_CUSTOM_DOMAIN


class User(AbstractUser):
    #: Playlists
    playlists: Union[Playlist, Manager]
    #: Events
    events: Union[Event, Manager]


@receiver(post_save, sender=User)
def user_post_save(instance: User, created, **kwargs):
    if not created:
        return

    favourites_playlist = Playlist.objects.create(
        name='Favourites',
        type=Playlist.Types.default,
        access_type=Playlist.AccessTypes.private,
        author=instance
    )
    instance.playlists.add(favourites_playlist)


def audio_file_validator(file: FieldFile):
    # allowed_extensions = TrackFile.Extensions.names
    allowed_extensions = TrackFile.Extensions.names
    file_extension = file.name.split('.')[-1]
    if file_extension not in allowed_extensions:
        raise ValidationError(
            'Audio file wrong extension',
            params={'value': file_extension},
        )


class Artist(models.Model):
    #: Artist name
    name = models.CharField(max_length=100)
    #: Artis tracks
    tracks: Union[Track, Manager]

    def __str__(self):
        return self.name


class Track(models.Model):
    #: Track name
    name: str = models.CharField(max_length=150, unique=True)
    #: Track Files
    files: Union[TrackFile, Manager]
    #: Track artist
    artist: Artist = models.ForeignKey(Artist, models.CASCADE, related_name='tracks')

    def __str__(self):
        return self.name


class TrackFile(models.Model):
    class Extensions(models.TextChoices):
        """Allowed extensions"""
        mp3 = 'mp3'
        flac = 'flac'

    #: Track file
    file: Union[FileIO[bytes], FieldFile] = models.FileField(
        upload_to='music',
        validators=[audio_file_validator],
        help_text=f'Send highest quality file, lowest will be make automatically<br>'
                  f'Allowed:<br> {"<br>".join(Extensions.names)}'
    )
    #: Track file extension
    extension: Extensions = models.CharField(max_length=50, choices=Extensions.choices, blank=True, null=True)
    #: Track duration in seconds
    duration: float = models.FloatField(blank=True, null=True)
    #: Track instance
    track: Track = models.ForeignKey(Track, models.SET_NULL, null=True, blank=True, related_name='files')

    def __str__(self):
        return f'{self.track.name} - {self.extension}'


@receiver(post_save, sender=TrackFile)
def file_post_save(instance: TrackFile, created, *args, **kwargs):
    post_save.disconnect(file_post_save, sender=TrackFile)
    if instance.file:
        file_extension = instance.file.name.split('.')[-1]
        if AWS_S3_CUSTOM_DOMAIN:
            file_path = "https://" + AWS_S3_CUSTOM_DOMAIN + "/" + str(instance.file.name)
        else:
            file_path = instance.file.path

        ffprobe_duration_cmd = [
            'ffprobe',
            '-i', file_path,
            '-show_entries', 'format=duration',
            '-v', 'quiet',
            '-of', 'csv=p=0'
        ]
        ffprobe_duration_process = subprocess.Popen(ffprobe_duration_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ffprobe_duration_output, ffprobe_duration_error = ffprobe_duration_process.communicate()

        if ffprobe_duration_error:
            print("Can't get duration:", ffprobe_duration_error.decode('utf-8'))
            return
        
        file_duration = float(ffprobe_duration_output.decode('utf-8').strip())

        instance.duration = file_duration
        instance.extension = file_extension
        instance.save()

        mp3_path = file_path.replace('.flac', '.mp3')
        mp3_name = instance.file.name.replace('.flac', '.mp3')

        _, export_not_exist = TrackFile.objects.get_or_create(
            id=instance.id + 1,
            track=instance.track,
            duration=instance.duration,
            extension=TrackFile.Extensions.mp3,
            file=mp3_name
        )

        if export_not_exist:
            ffmpeg_mp3_cmd = [
                'ffmpeg',
                '-hide_banner',
                '-loglevel', 'error',
                '-i', file_path,
                '-b:a', '320K',
                '-vn',
                '-f', 'mp3',
                '-'
            ]
            ffmpeg_mp3_process = subprocess.Popen(ffmpeg_mp3_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ffmpeg_mp3_output, ffmpeg_mp3_error = ffmpeg_mp3_process.communicate()
            if ffmpeg_mp3_error:
                print("Can't create MP3 file:", ffmpeg_mp3_error.decode('utf-8'))
            else:
                ffmpeg_mp3_content = ContentFile(ffmpeg_mp3_output)
                if AWS_S3_CUSTOM_DOMAIN:
                    default_storage.save(mp3_name, ffmpeg_mp3_content)
                else:
                    default_storage.save(mp3_path, ffmpeg_mp3_content)

    post_save.connect(file_post_save, sender=TrackFile)


@receiver(post_delete, sender=TrackFile)
def file_post_delete(instance: TrackFile, *args, **kwargs):
    try:
        instance.file.delete()
    except FileNotFoundError:
        ...


class Playlist(models.Model):
    class Types:
        default = 'default'  #: Default playlist e.g. favourites
        custom = 'custom'  #: Custom playlist, created by user
        temporary = 'temporary'  #: System playlist, not shown at any list from API

    TypesChoice = (
        (Types.default, 'Default'),
        (Types.custom, 'Custom'),
        (Types.temporary, 'Temporary'),
    )

    class AccessTypes:
        public = 'public'  #: Everyone can access
        private = 'private'  #: Only invited users can access

    AccessTypesChoice = (
        (AccessTypes.public, 'Public'),
        (AccessTypes.private, 'Private'),
    )

    #: Playlist name
    name = models.CharField(max_length=150, default=str(uuid.uuid4))
    #: Playlist type
    type: Types = models.CharField(max_length=50, choices=TypesChoice, default=Types.custom)
    #: Playlist access type
    access_type: AccessTypes = models.CharField(max_length=50, choices=AccessTypesChoice, default=AccessTypes.public)
    #: Playlist`s author
    author: User = models.ForeignKey(User, models.CASCADE, related_name='playlists')
    #: Users accessed to this playlist
    playlist_access_users: Union[PlaylistAccess, Manager]
    #: Tracks in this playlist
    tracks: Union[PlaylistTrack, Manager]

    def __str__(self):
        return f"{self.author}'s playlist"


class PlaylistTrack(models.Model):
    track: Track = models.ForeignKey(Track, models.CASCADE)  #: Track object
    order: int = models.IntegerField(default=0)  #: Track order in playlist
    playlist = models.ForeignKey(Playlist, models.CASCADE, related_name='tracks')

    class Meta:
        ordering = ['order']


class PlaylistAccess(models.Model):
    #: User who access to playlist
    user: User = models.ForeignKey(User, models.CASCADE)
    #: Playlist instance
    playlist: Playlist = models.ForeignKey(Playlist, models.CASCADE, related_name='playlist_access_users')


class SessionTrack(models.Model):
    class States:
        stopped = 'stopped'
        playing = 'playing'
        paused = 'paused'

    StatesChoice = (
        (States.stopped, 'Stopped'),
        (States.playing, 'Playing'),
        (States.paused, 'Paused'),
    )

    #: Session track state
    state: States = models.CharField(max_length=50, choices=StatesChoice, default=States.stopped)
    #: Track object
    track: Track = models.ForeignKey(Track, models.CASCADE)
    #: Votes user for next play
    votes: Union[List[User], Manager] = models.ManyToManyField(User)
    #: Votes count for next play
    votes_count: int = models.PositiveIntegerField(default=0)
    #: Track time progress from duration
    progress: float = models.FloatField(default=0)
    #: Tracks order in queue
    order: int = models.IntegerField(default=0)

    class Meta:
        ordering = ['-votes_count', 'order']

    def __str__(self):
        return f'{self.track.name}-{self.state}-{self.order}'


class PlayerSession(models.Model):
    class Modes:
        repeat = 'repeat'  #: Repeat single track for loop
        normal = 'normal'  #: Normal mode, tracks play in usual order

    ModeChoice = (
        (Modes.normal, 'Normal'),
        (Modes.repeat, 'Repeat one song'),
    )

    #: PlaylistChanged object
    playlist: Union[Playlist, Manager] = models.ForeignKey(Playlist, models.CASCADE)
    #: Track queue
    track_queue: Union[List[SessionTrack], Manager] = models.ManyToManyField(SessionTrack)
    #: Player Session mode
    mode: Modes = models.CharField(max_length=50, choices=ModeChoice, default=Modes.normal)
    #: Player Session author
    author: User = models.ForeignKey(User, models.CASCADE)


@receiver(post_save, sender=PlayerSession)
def player_session_post_save(instance: PlayerSession, created, **kwargs):
    if not created:
        return

    PlayerSession.objects.filter(author=instance.author).exclude(id=instance.id).delete()

    playlist_tracks: List[PlaylistTrack] = instance.playlist.tracks.all()
    for i, playlist_track in enumerate(playlist_tracks):
        session_track = SessionTrack.objects.create(track=playlist_track.track, order=i)
        instance.track_queue.add(session_track)


class Event(models.Model):
    class AccessTypes(models.TextChoices):
        public = 'public', 'Public'  #: Everyone can access
        private = 'private', 'Private'  #: Only invited users can access

    #: Event name
    name = models.CharField(max_length=150)
    #: Event`s author
    author: User = models.ForeignKey(User, models.CASCADE, related_name='events')
    #: Event access type
    access_type: AccessTypes = models.CharField(max_length=50, choices=AccessTypes.choices, default=AccessTypes.public)
    #: Event start date
    start_date = models.DateTimeField()
    #: Event end date
    end_date = models.DateTimeField()
    #: Event finished flag
    is_finished = models.BooleanField(default=False)
    #: Shared player session
    player_session: PlayerSession = models.ForeignKey(
        PlayerSession, models.CASCADE, null=True, blank=True, default=None
    )
    #: Users accessed to this event
    event_access_users: Union[PlaylistAccess, Manager]

    class Meta:
        ordering = ['start_date']

    def __str__(self):
        return self.name


class EventAccess(models.Model):
    class AccessMode(models.TextChoices):
        guest = 'guest', 'Only view'
        moderator = 'moderator', 'Edit playlist, invite users'
        administrator = 'administrator', "Edit playlist, invite users, change user's access mode"

    #: User access mode
    access_mode: AccessMode = models.CharField(max_length=50, choices=AccessMode.choices, default=AccessMode.guest)
    #: User who access to event
    user: User = models.ForeignKey(User, models.CASCADE)
    #: Event instance
    event: Event = models.ForeignKey(Event, models.CASCADE, related_name='event_access_users')

