# Changelog

## V2.0.0

### Sentry Modernization
 
- **MAJOR** Upgraded the core execution and Docker environment from Python 2.7 to Python 3.12.
- Eradicated legacy Python 2 syntax (replaced `unicode()` and `long` with native Python 3 `str()` and `int`, and converted all `print` statements to functions).
- Stripped the `six` compatibility library and modernized dictionary iterations by replacing `six.iteritems()` with native `.items()`.
- Fixed dynamic SQL query generation by explicitly importing `reduce` from the `functools` library.
- Replaced deprecated `httplib` and `urlparse` libraries with the modern `http.client` and `urllib.parse` standards.
- Prevented Python 3 iterator exhaustion by explicitly wrapping `map()` and `filter()` generator calls in `list()`.
- Aggressively minified vertical whitespace across all core files for deployment optimization.
- Updated Docker Compose, database initialization, and Redis configurations to reflect the `sentry` service name and postgres user.
- Renamed the web dashboard's Flask application wrapper to `sentry_app` to prevent module shadowing.

### Features

- Added `archive extend` command which extends the duration of a current or expired archive
- Added some information to the guild overview/information page on the dashboard
- Added a spam bucket for max upper case letters (`max_upper_case`)
- Added `group_confirm_reactions` option to admin configuration, when toggled to true it will respond to !join and !leave group commands with only a reaction
- Added the ability to "snooze" reminders via reactions
- Added statistics around message latency
- Added a channel mention within the `SPAM_DEBUG` modlog event

### Bugfixes

- Fixed the response text of the `seen` command
- Fixed the infractions tab not showing up in the sidebar when viewing the config
- Fixed carrige returns not being counted as new lines in spam
- Fixed a bug with `mute` that would not allow a mute with no duration or reason to be applied
- Fixed case where long message deletions would not be properly logged (they are now truncated properly by the modlog)

## V1.2.0

### Features

- Twitch plugin added, can be used to track and notify a server of streams going online. (Currently early beta)

## V1.1.1

- Removed some utilities commands that didn't fit Sentry's goal
- Etc SQL changes

## V1.1.0

### Features

- **MAJOR** Added support for audit-log reasons withing various admin actions. This will log the reason you provide for kicks/bans/mutes/etc within the Discord audit-log.
- **MAJOR** !mute behavior has changed. If a valid duration string is the first part of the reason, a !mute command is transformed into a tempmute. This should help resolve a common mistake people make.
- !join and !leave will no longer respond if no group roles are specified within the admin config
- Added a SQL command for global admins to graph word usage in a server.

### Bugfixes

- Fixed reloading of SQLPlugin in development
- Fixed some user images causing `get_dominant_colors` to return an incorrect value causing a command exception
- Fixed error case in modlog when handling VoiceStateUpdate
- Fixed a case where a user could not save the webconfig because the web access object had their ID stored as a string
- Fixed censor throwing errors when a message which was censored was already deleted

## V1.0.5

Similar changes to v1.0.4

## V1.0.4

### Bugfixes

- Fixed invalid function call causing errors w/ CHANGE_USERNAME event

## V1.0.3

### Features

- Added two new modlog events, `MEMBER_TEMPMUTE_EXPIRE` and `MEMBER_TEMPBAN_EXPIRE` which are triggered when their respective infractions expire

### Bugfixes

- Fixed cases where certain modlog channels could become stuck due to transient Discord issues
- Fixed cases where content in certain censor filters would be ignored due to its casing, censor now ignores all casing in filters within its config

### Etc

- Don't leave the SENTRY_GUILD_ID, its special (and not doing this makes it impossible to bootstrap the bot otherwise)
- Improved the performance of !stats

## V1.0.2

### Bugfixes

- Fixed the user in a ban/forceban's modlog message being `<UNKNOWN>`. The modlog entry will now contain their ID if Sentry cannot resolve further user information
- Fixed the duration of unlocking a role being 6 minutes instead of 5 minutes like the response message said
- Fixed some misc errors thrown when passing webhook messages to censor/spam plugins
- Fixed case where Sentry guild access was not being properly synced due to invalid data being passed in the web configuration for some guilds
- Fixed the documentation URL being outdated
- Fixed some commands being incorrectly exposed publically
- Fixed the ability to revoke or change ones own roles within the configuration

### Etc

- Removed ignored_channels, this concept is no longer (and hasn't been for a long time) used.
- Improved the performance (and formatting) around the !info command

## V1.0.1

### Bugfixes

- Fixed admin add/rmv role being able to operate on role that matched the command executors highest role.
- Fixed error triggered when removing debounces that where already partially-removed
- Fixed add/remove role throwing a command error when attempting to execute the modlog portion of their code.
- Fixed case where User.tempmute was called externally (e.g. by spam) for a guild without a mute role setup

## V1.0.0

### **BREAKING** Group Permissions Protection

This update includes a change to the way admin-groups (aka joinable roles) work. When a user attempts to join a group now, Sentry will check and confirm the role does not give any unwanted permissions (e.g. _anything_ elevated). This check can not be skipped or disabled in the configuration. Groups are explicitly meant to give cosmetic or channel-based permissions to users, and should _never_ include elevated permissions. In the case that a group role somehow is created or gets permissions, this prevents any users from using Sentry as an elevation attack. Combined with guild role locking, this should prevent almost all possible permission escalation attacks.

### Guild Role Locking

This new feature allows Sentry to lock-down a role, completely preventing/reverting updates to it. Roles can be unlocked by an administrator using the `!role unlock <role_id>` command, or by removing them from the config. The intention of this feature is to help locking down servers from permission escalation attacks. Role locking should be enabled for all roles that do not and should not change regularly, and for added protection you can disable the unlock command within your config.

```yaml
plugins:
  admin:
    locked_roles: [ROLE_ID_HERE]