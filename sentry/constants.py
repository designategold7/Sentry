import os
import re
import yaml
from disco.types.user import GameType, Status
try:
    with open('constants.yaml', 'r') as f:
        _loaded = yaml.safe_load(f.read())
        _config = _loaded.get('constants', {}) if _loaded else {}
except FileNotFoundError:
    _config = {}
SENTRY_GUILD_ID = _config.get('SENTRY_GUILD_ID', 1473771000897339506)
SENTRY_USER_ROLE_ID = _config.get('SENTRY_USER_ROLE_ID', 1473774350330237028)
SENTRY_CONTROL_CHANNEL = _config.get('SENTRY_CONTROL_CHANNEL', 1474842159088799954)
GREEN_TICK_EMOJI_ID = _config.get('GREEN_TICK_EMOJI_ID', 305231298799206401)
RED_TICK_EMOJI_ID = _config.get('RED_TICK_EMOJI_ID', 305231335512080385)
CDN_URL = _config.get('CDN_URL', 'https://twemoji.maxcdn.com/2/72x72/{}.png')
GREEN_TICK_EMOJI = 'green_tick:{}'.format(GREEN_TICK_EMOJI_ID)
RED_TICK_EMOJI = 'red_tick:{}'.format(RED_TICK_EMOJI_ID)
STAR_EMOJI = '\U00002B50'
SNOOZE_EMOJI = '\U0001f4a4'
STATUS_EMOJI = {
    Status.ONLINE: ':status_online:305889169811439617',
    Status.IDLE: ':status_away:305889079222992896',
    Status.DND: ':status_dnd:305889053255925760',
    Status.OFFLINE: ':status_offline:305889028996071425',
    GameType.STREAMING: ':status_streaming:305889126463569920',
}
INVITE_LINK_RE = re.compile(r'(discordapp.com/invite|discord.me|discord.gg)(?:/#)?(?:/invite)?/([a-z0-9\-]+)', re.I)
URL_RE = re.compile(r'(https?://[^\s]+)')
EMOJI_RE = re.compile(r'<:(.+):([0-9]+)>')
USER_MENTION_RE = re.compile('<@!?([0-9]+)>')
ERR_UNKNOWN_MESSAGE = 10008
YEAR_IN_SEC = 60 * 60 * 24 * 365
try:
    with open('data/badwords.txt', 'r') as f:
        BAD_WORDS = f.readlines()
except FileNotFoundError:
    BAD_WORDS = []