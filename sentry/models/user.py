import asyncio
from datetime import datetime, timezone
from holster.enum import Enum
from peewee import BigIntegerField, IntegerField, SmallIntegerField, TextField, BooleanField, DateTimeField
from playhouse.postgres_ext import BinaryJSONField
from sentry.sql import BaseModel

@BaseModel.register
class User(BaseModel):
    user_id = BigIntegerField(primary_key=True)
    username = TextField()
    discriminator = SmallIntegerField()
    avatar = TextField(null=True)
    bot = BooleanField()
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    admin = BooleanField(default=False)
    
    SQL = '''
        CREATE INDEX IF NOT EXISTS users_username_trgm ON users USING gin(username gin_trgm_ops);
    '''

    class Meta:
        db_table = 'users'
        indexes = (
            (('user_id', 'username', 'discriminator'), True),
        )

    def serialize(self, us=False):
        base = {
            'id': str(self.user_id),
            'username': self.username,
            'discriminator': self.discriminator,
            'avatar': self.avatar,
            'bot': self.bot,
        }
        if us:
            base['admin'] = self.admin
        return base

    @property
    def id(self):
        return self.user_id

    @classmethod
    def ensure(cls, user, should_update=True):
        return cls.from_disco_user(user, should_update)

    @classmethod
    def with_id(cls, uid):
        try:
            return User.get(user_id=uid)
        except User.DoesNotExist:
            return None

    @classmethod
    def from_disco_user(cls, user, should_update=True):
        avatar_hash = user.avatar.key if getattr(user, 'avatar', None) else None
        # d.py represents global names with discriminator '0'
        discrim = int(user.discriminator) if getattr(user, 'discriminator', None) and str(user.discriminator).isdigit() else 0
        
        obj, _ = cls.get_or_create(
            user_id=user.id,
            defaults={
                'username': getattr(user, 'name', user.display_name),
                'discriminator': discrim,
                'avatar': avatar_hash,
                'bot': getattr(user, 'bot', False)
            })
            
        if should_update:
            updates = {}
            if obj.username != getattr(user, 'name', user.display_name):
                updates['username'] = getattr(user, 'name', user.display_name)
            if obj.discriminator != discrim:
                updates['discriminator'] = discrim
            if obj.avatar != avatar_hash:
                updates['avatar'] = avatar_hash
                
            if updates:
                cls.update(**updates).where(User.user_id == user.id).execute()
        return obj

    def get_avatar_url(self, fmt='webp', size=1024):
        if not self.avatar:
            return None
        return f'https://cdn.discordapp.com/avatars/{self.user_id}/{self.avatar}.{fmt}?size={size}'

    def __str__(self):
        return f'{self.username}#{str(self.discriminator).zfill(4)}'

@BaseModel.register
class Infraction(BaseModel):
    Types = Enum(
        'MUTE',
        'KICK',
        'TEMPBAN',
        'SOFTBAN',
        'BAN',
        'TEMPMUTE',
        'UNBAN',
        'TEMPROLE',
        'WARNING',
        bitmask=False,
    )
    
    guild_id = BigIntegerField()
    user_id = BigIntegerField()
    actor_id = BigIntegerField(null=True)
    type_ = IntegerField(db_column='type')
    reason = TextField(null=True)
    metadata = BinaryJSONField(default={})
    expires_at = DateTimeField(null=True)
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    active = BooleanField(default=True)

    class Meta:
        db_table = 'infractions'
        indexes = (
            (('guild_id', 'user_id'), False),
        )

    def serialize(self, guild=None, user=None, actor=None, include_metadata=False):
        base = {
            'id': str(self.id),
            'guild': (guild and guild.serialize()) or {'id': str(self.guild_id)},
            'user': (user and user.serialize()) or {'id': str(self.user_id)},
            'actor': (actor and actor.serialize()) or {'id': str(self.actor_id)},
            'reason': self.reason,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'active': self.active,
        }
        base['type'] = {
            'id': self.type_,
            'name': next(i.name for i in Infraction.Types.attrs if i.index == self.type_)
        }
        if include_metadata:
            base['metadata'] = self.metadata
        return base

    @staticmethod
    def admin_config(ctx):
        return getattr(ctx.base_config.plugins, 'admin', None)

    # All Moderation actions are now ASYNC to safely yield to the d.py event loop
    @classmethod
    async def temprole(cls, plugin, ctx, member, role_id, reason, expires_at):
        await asyncio.to_thread(User.from_disco_user, member._user if hasattr(member, '_user') else member)
        
        role = ctx.guild.get_role(role_id)
        if role:
            await member.add_roles(role, reason=reason)
            
        def db_insert():
            cls.create(
                guild_id=ctx.guild.id,
                user_id=member.id,
                actor_id=ctx.author.id,
                type_=cls.Types.TEMPROLE,
                reason=reason,
                expires_at=expires_at,
                metadata={'role': role_id})
        await asyncio.to_thread(db_insert)

    @classmethod
    async def kick(cls, plugin, ctx, member, reason):
        from sentry.plugins.modlog import Actions
        await asyncio.to_thread(User.from_disco_user, member._user if hasattr(member, '_user') else member)
        
        modlog = plugin.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx, ['on_member_remove'], user_id=member.id)
            
        await member.kick(reason=reason)
        
        if modlog:
            await modlog.log_action_ext(
                Actions.MEMBER_KICK,
                ctx.guild.id,
                member=member,
                actor=str(ctx.author) if ctx.author.id != member.id else 'Automatic',
                reason=reason or 'no reason'
            )
            
        def db_insert():
            cls.create(
                guild_id=member.guild.id,
                user_id=member.id,
                actor_id=ctx.author.id,
                type_=cls.Types.KICK,
                reason=reason)
        await asyncio.to_thread(db_insert)

    @classmethod
    async def tempban(cls, plugin, ctx, member, reason, expires_at):
        from sentry.plugins.modlog import Actions
        await asyncio.to_thread(User.from_disco_user, member._user if hasattr(member, '_user') else member)
        
        modlog = plugin.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx, ['on_member_remove', 'on_member_ban'], user_id=member.id)
            
        await member.ban(reason=reason)
        
        if modlog:
            await modlog.log_action_ext(
                Actions.MEMBER_TEMPBAN,
                ctx.guild.id,
                member=member,
                actor=str(ctx.author) if ctx.author.id != member.id else 'Automatic',
                reason=reason or 'no reason',
                expires=expires_at,
            )
            
        def db_insert():
            cls.create(
                guild_id=member.guild.id,
                user_id=member.id,
                actor_id=ctx.author.id,
                type_=cls.Types.TEMPBAN,
                reason=reason,
                expires_at=expires_at)
        await asyncio.to_thread(db_insert)

    @classmethod
    async def softban(cls, plugin, ctx, member, reason):
        from sentry.plugins.modlog import Actions
        await asyncio.to_thread(User.from_disco_user, member._user if hasattr(member, '_user') else member)
        
        modlog = plugin.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx, ['on_member_remove', 'on_member_ban', 'on_member_unban'], user_id=member.id)
            
        await member.ban(delete_message_seconds=604800, reason=reason) # 7 days
        await member.unban(reason=reason)
        
        if modlog:
            await modlog.log_action_ext(
                Actions.MEMBER_SOFTBAN,
                ctx.guild.id,
                member=member,
                actor=str(ctx.author) if ctx.author.id != member.id else 'Automatic',
                reason=reason or 'no reason'
            )
            
        def db_insert():
            cls.create(
                guild_id=member.guild.id,
                user_id=member.id,
                actor_id=ctx.author.id,
                type_=cls.Types.SOFTBAN,
                reason=reason)
        await asyncio.to_thread(db_insert)

    @classmethod
    async def ban(cls, plugin, ctx, member, reason, guild):
        from sentry.plugins.modlog import Actions
        if isinstance(member, int):
            user_id = member
            member_str = str(user_id)
        else:
            await asyncio.to_thread(User.from_disco_user, member._user if hasattr(member, '_user') else member)
            user_id = member.id
            member_str = str(member)
            
        modlog = plugin.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx, ['on_member_remove', 'on_member_ban'], user_id=user_id)
            
        import discord
        await guild.ban(discord.Object(id=user_id), reason=reason)
        
        if modlog:
            await modlog.log_action_ext(
                Actions.MEMBER_BAN,
                ctx.guild.id,
                user=member_str,
                user_id=user_id,
                actor=str(ctx.author) if ctx.author.id != user_id else 'Automatic',
                reason=reason or 'no reason'
            )
            
        def db_insert():
            cls.create(
                guild_id=guild.id,
                user_id=user_id,
                actor_id=ctx.author.id,
                type_=cls.Types.BAN,
                reason=reason)
        await asyncio.to_thread(db_insert)

    @classmethod
    async def warn(cls, plugin, ctx, member, reason, guild):
        from sentry.plugins.modlog import Actions
        await asyncio.to_thread(User.from_disco_user, member._user if hasattr(member, '_user') else member)
        user_id = member.id
        
        def db_insert():
            cls.create(
                guild_id=guild.id,
                user_id=user_id,
                actor_id=ctx.author.id,
                type_=cls.Types.WARNING,
                reason=reason)
        await asyncio.to_thread(db_insert)
        
        modlog = plugin.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.log_action_ext(
                Actions.MEMBER_WARNED,
                ctx.guild.id,
                member=member,
                actor=str(ctx.author) if ctx.author.id != member.id else 'Automatic',
                reason=reason or 'no reason'
            )

    @classmethod
    async def mute(cls, plugin, ctx, member, reason):
        from sentry.plugins.modlog import Actions
        admin_config = cls.admin_config(ctx)
        role_id = admin_config.mute_role if admin_config else None
        role = ctx.guild.get_role(role_id) if role_id else None
        
        if role:
            modlog = plugin.bot.get_cog('ModLogPlugin')
            if modlog:
                await modlog.create_debounce(ctx, ['on_member_update'], user_id=member.id, role_id=role_id)
                
            await member.add_roles(role, reason=reason)
            
            if modlog:
                await modlog.log_action_ext(
                    Actions.MEMBER_MUTED,
                    ctx.guild.id,
                    member=member,
                    actor=str(ctx.author) if ctx.author.id != member.id else 'Automatic',
                    reason=reason or 'no reason'
                )
                
            def db_insert():
                cls.create(
                    guild_id=ctx.guild.id,
                    user_id=member.id,
                    actor_id=ctx.author.id,
                    type_=cls.Types.MUTE,
                    reason=reason,
                    metadata={'role': role_id})
            await asyncio.to_thread(db_insert)

    @classmethod
    async def tempmute(cls, plugin, ctx, member, reason, expires_at):
        from sentry.plugins.modlog import Actions
        admin_config = cls.admin_config(ctx)
        role_id = admin_config.mute_role if admin_config else None
        role = ctx.guild.get_role(role_id) if role_id else None
        
        if not role:
            print(f'Cannot tempmute member {member.id}, no tempmute role found')
            return
            
        modlog = plugin.bot.get_cog('ModLogPlugin')
        if modlog:
            await modlog.create_debounce(ctx, ['on_member_update'], user_id=member.id, role_id=role_id)
            
        await member.add_roles(role, reason=reason)
        
        if modlog:
            await modlog.log_action_ext(
                Actions.MEMBER_TEMP_MUTED,
                ctx.guild.id,
                member=member,
                actor=str(ctx.author) if ctx.author.id != member.id else 'Automatic',
                reason=reason or 'no reason',
                expires=expires_at,
            )
            
        def db_insert():
            cls.create(
                guild_id=ctx.guild.id,
                user_id=member.id,
                actor_id=ctx.author.id,
                type_=cls.Types.TEMPMUTE,
                reason=reason,
                expires_at=expires_at,
                metadata={'role': role_id})
        await asyncio.to_thread(db_insert)

    # This remains synchronous so it can be called safely via asyncio.to_thread where appropriate
    @classmethod
    def clear_active(cls, ctx, user_id, types):
        guild_id = getattr(ctx, 'guild_id', getattr(getattr(ctx, 'guild', None), 'id', None))
        if not guild_id: return False
        
        return cls.update(active=False).where(
            (cls.guild_id == guild_id) &
            (cls.user_id == user_id) &
            (cls.type_ << types) &
            (cls.active == 1)
        ).execute() >= 1

@BaseModel.register
class StarboardBlock(BaseModel):
    guild_id = BigIntegerField()
    user_id = BigIntegerField()
    actor_id = BigIntegerField()

    class Meta:
        db_table = 'starboard_blocks'
        indexes = (
            (('guild_id', 'user_id'), True),
        )