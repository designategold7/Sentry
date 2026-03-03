import yaml
import logging
import asyncio
from peewee import (
    BigIntegerField, CharField, TextField, BooleanField, DateTimeField, CompositeKey, BlobField
)
from holster.enum import Enum
from datetime import datetime, timezone
from playhouse.postgres_ext import BinaryJSONField, ArrayField
from sentry.sql import BaseModel
from sentry.redis import emit
from sentry.models.user import User

log = logging.getLogger(__name__)

@BaseModel.register
class Guild(BaseModel):
    WhitelistFlags = Enum(
        'MUSIC',
        'MODLOG_CUSTOM_FORMAT',
        bitmask=False
    )
    guild_id = BigIntegerField(primary_key=True)
    owner_id = BigIntegerField(null=True)
    name = TextField(null=True)
    icon = TextField(null=True)
    splash = TextField(null=True)
    region = TextField(null=True)
    last_ban_sync = DateTimeField(null=True)
    
    config = BinaryJSONField(null=True)
    config_raw = BlobField(null=True)
    enabled = BooleanField(default=True)
    whitelist = BinaryJSONField(default=[])
    added_at = DateTimeField(default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    class Meta:
        db_table = 'guilds'

    @classmethod
    def with_id(cls, guild_id):
        return cls.get(guild_id=guild_id)

    @classmethod
    def setup(cls, guild):
        # Handle d.py Asset mapping
        icon_url = guild.icon.key if guild.icon else None
        splash_url = guild.splash.key if guild.splash else None
        
        return cls.create(
            guild_id=guild.id,
            owner_id=guild.owner_id,
            name=guild.name,
            icon=icon_url,
            splash=splash_url,
            region=getattr(guild, 'preferred_locale', None),
            config={'web': {guild.owner_id: 'admin'}},
            config_raw='')

    def is_whitelisted(self, flag):
        return int(flag) in self.whitelist

    def update_config(self, actor_id, raw):
        from sentry.types.guild import GuildConfig
        parsed = yaml.safe_load(raw)
        GuildConfig(parsed).validate()
        
        GuildConfigChange.create(
            user_id=actor_id,
            guild_id=self.guild_id,
            before_raw=self.config_raw,
            after_raw=raw)
            
        self.update(config=parsed, config_raw=raw).where(Guild.guild_id == self.guild_id).execute()
        self.emit('GUILD_UPDATE')

    def emit(self, action, **kwargs):
        emit(action, id=self.guild_id, **kwargs)

    def sync(self, guild):
        updates = {}
        icon_url = guild.icon.key if guild.icon else None
        splash_url = guild.splash.key if guild.splash else None
        locale = getattr(guild, 'preferred_locale', None)
        
        mappings = {
            'owner_id': guild.owner_id,
            'name': guild.name,
            'icon': icon_url,
            'splash': splash_url,
            'region': locale
        }
        
        for key, val in mappings.items():
            if val != getattr(self, key):
                updates[key] = val
                
        if updates:
            Guild.update(**updates).where(Guild.guild_id == self.guild_id).execute()

    def get_config(self, refresh=False):
        from sentry.types.guild import GuildConfig
        if refresh:
            self.config = Guild.select(Guild.config).where(Guild.guild_id == self.guild_id).get().config
            
        if refresh or not hasattr(self, '_cached_config'):
            try:
                self._cached_config = GuildConfig(self.config)
            except Exception:
                log.exception('Failed to load config for Guild %s, invalid: ', self.guild_id)
                return None
        return self._cached_config

    async def sync_bans(self, guild):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await asyncio.to_thread(
            Guild.update(last_ban_sync=now).where((Guild.guild_id == self.guild_id)).execute
        )
        
        try:
            # d.py bans iterator mapped securely
            bans = [ban async for ban in guild.bans(limit=None)]
        except Exception:
            log.exception('sync_bans failed:')
            return
            
        log.info(f'Syncing {len(bans)} bans for guild {guild.id}')
        
        def db_update():
            GuildBan.delete().where(
                (~(GuildBan.user_id << [b.user.id for b in bans])) &
                (GuildBan.guild_id == guild.id)
            ).execute()
            for ban in bans:
                GuildBan.ensure(guild, ban.user, getattr(ban, 'reason', None))
                
        await asyncio.to_thread(db_update)

    def serialize(self):
        base = {
            'id': str(self.guild_id),
            'owner_id': str(self.owner_id),
            'name': self.name,
            'icon': self.icon,
            'splash': self.splash,
            'region': self.region,
            'enabled': self.enabled,
            'whitelist': self.whitelist
        }
        if hasattr(self, 'role'):
            base['role'] = self.role
        return base

@BaseModel.register
class GuildEmoji(BaseModel):
    emoji_id = BigIntegerField(primary_key=True)
    guild_id = BigIntegerField()
    name = CharField(index=True)
    require_colons = BooleanField()
    managed = BooleanField()
    roles = ArrayField(BigIntegerField, default=[], null=True)
    deleted = BooleanField(default=False)
    
    class Meta:
        db_table = 'guild_emojis'

    @classmethod
    def from_disco_guild_emoji(cls, emoji, guild_id=None):
        try:
            ge = cls.get(emoji_id=emoji.id)
            new = False
        except cls.DoesNotExist:
            ge = cls(emoji_id=emoji.id)
            new = True
            
        ge.guild_id = guild_id or emoji.guild_id
        ge.name = emoji.name
        ge.require_colons = getattr(emoji, 'require_colons', False)
        ge.managed = getattr(emoji, 'managed', False)
        ge.roles = [r.id for r in emoji.roles] if hasattr(emoji, 'roles') else []
        ge.save(force_insert=new)
        return ge

@BaseModel.register
class GuildBan(BaseModel):
    user_id = BigIntegerField()
    guild_id = BigIntegerField()
    reason = TextField(null=True)
    
    class Meta:
        db_table = 'guild_bans'
        primary_key = CompositeKey('user_id', 'guild_id')

    @classmethod
    def ensure(cls, guild, user, reason=None):
        User.ensure(user)
        obj, _ = cls.get_or_create(guild_id=guild.id, user_id=user.id, defaults=dict({
            'reason': reason,
        }))
        return obj

@BaseModel.register
class GuildConfigChange(BaseModel):
    user_id = BigIntegerField(null=True)
    guild_id = BigIntegerField()
    before_raw = BlobField(null=True)
    after_raw = BlobField()
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    
    class Meta:
        db_table = 'guild_config_changes'
        indexes = (
            (('user_id', 'guild_id'), False),
        )

    def rollback_to(self):
        Guild.update(
            config_raw=self.after_raw,
            config=yaml.safe_load(self.after_raw)
        ).where(Guild.guild_id == self.guild_id).execute()

    def revert(self):
        Guild.update(
            config_raw=self.before_raw,
            config=yaml.safe_load(self.before_raw)
        ).where(Guild.guild_id == self.guild_id).execute()

@BaseModel.register
class GuildMemberBackup(BaseModel):
    user_id = BigIntegerField()
    guild_id = BigIntegerField()
    nick = CharField(null=True)
    roles = ArrayField(BigIntegerField, default=[], null=True)
    mute = BooleanField(null=True)
    deaf = BooleanField(null=True)
    
    class Meta:
        db_table = 'guild_member_backups'
        primary_key = CompositeKey('user_id', 'guild_id')

    @classmethod
    def remove_role(cls, guild_id, user_id, role_id):
        sql = '''
            UPDATE guild_member_backups
                SET roles = array_remove(roles, %s)
            WHERE
                guild_member_backups.guild_id = %s AND
                guild_member_backups.user_id = %s AND
                guild_member_backups.roles @> ARRAY[%s]
        '''
        cls.raw(sql, role_id, guild_id, user_id, role_id).execute()

    @classmethod
    def create_from_member(cls, member):
        cls.delete().where(
            (cls.user_id == member.id) &
            (cls.guild_id == member.guild.id)
        ).execute()
        
        voice_state = getattr(member, 'voice', None)
        return cls.create(
            user_id=member.id,
            guild_id=member.guild.id,
            nick=member.nick,
            roles=[r.id for r in member.roles],
            mute=getattr(voice_state, 'mute', False),
            deaf=getattr(voice_state, 'deaf', False),
        )

@BaseModel.register
class GuildVoiceSession(BaseModel):
    session_id = TextField()
    user_id = BigIntegerField()
    guild_id = BigIntegerField()
    channel_id = BigIntegerField()
    started_at = DateTimeField()
    ended_at = DateTimeField(default=None, null=True)
    
    class Meta:
        db_table = 'guild_voice_sessions'
        indexes = (
            (('session_id', 'user_id', 'guild_id', 'channel_id', 'started_at', 'ended_at', ), True),
            (('started_at', 'ended_at', ), False),
        )

    @classmethod
    def create_or_update(cls, before, after, member):
        # We enforce timezone-naive UTC for Peewee compatibility
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        session_id = getattr(after, 'session_id', 'unknown')
        
        if before and before.channel:
            GuildVoiceSession.update(
                ended_at=now
            ).where(
                (GuildVoiceSession.user_id == member.id) &
                (GuildVoiceSession.guild_id == member.guild.id) &
                (GuildVoiceSession.channel_id == before.channel.id) &
                (GuildVoiceSession.ended_at >> None)
            ).execute()
            
        if after and after.channel:
            GuildVoiceSession.insert(
                session_id=session_id,
                guild_id=member.guild.id,
                channel_id=after.channel.id,
                user_id=member.id,
                started_at=now,
            ).returning(GuildVoiceSession.id).on_conflict('DO NOTHING').execute()