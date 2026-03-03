from collections import namedtuple
from math import sqrt
import random
Point = namedtuple('Point', ('coords', 'n', 'ct'))
Cluster = namedtuple('Cluster', ('points', 'center', 'n'))
def get_points(img):
    points = []
    w, h = img.size
    for count, color in img.getcolors(w * h): points.append(Point(color, 3, count))
    return points
def rtoh(rgb):
    return '%s' % ''.join(('%02x' % p for p in rgb))
def get_dominant_colors(img, n=3):
    try:
        img.thumbnail((1024, 1024))
        points = get_points(img)
        clusters = kmeans(points, n, 1)
        rgbs = [list(map(int, c.center.coords)) for c in clusters]
        return list(map(rtoh, rgbs))
    except: return [rtoh((0, 0, 0))]
def get_dominant_colors_user(user, url=None):
    import requests
    from sentry.redis import rdb
    from PIL import Image
    from io import BytesIO
    key = f'avatar:color:{getattr(user.display_avatar, "key", user.display_avatar.url)}'
    if rdb.exists(key): return int(rdb.get(key))
    try:
        r = requests.get(url or user.display_avatar.url)
        r.raise_for_status()
        color = int(get_dominant_colors(Image.open(BytesIO(r.content)))[0], 16)
        rdb.set(key, color)
        return color
    except: return 0
def get_dominant_colors_guild(guild):
    import requests
    from sentry.redis import rdb
    from PIL import Image
    from io import BytesIO
    if not guild.icon: return 0
    key = f'guild:color:{guild.icon.key}'
    if rdb.exists(key): return int(rdb.get(key))
    try:
        r = requests.get(guild.icon.url)
        r.raise_for_status()
        color = int(get_dominant_colors(Image.open(BytesIO(r.content)))[0], 16)
        rdb.set(key, color)
        return color
    except: return 0
def euclidean(p1, p2):
    return sqrt(sum([(p1.coords[i] - p2.coords[i]) ** 2 for i in range(p1.n)]))
def calculate_center(points, n):
    vals = [0.0 for i in range(n)]
    plen = 0
    for p in points:
        plen += p.ct
        for i in range(n): vals[i] += (p.coords[i] * p.ct)
    return Point([(v / plen) for v in vals], n, 1)
def kmeans(points, k, min_diff):
    clusters = [Cluster([p], p, p.n) for p in random.sample(points, k)]
    while 1:
        plists = [[] for i in range(k)]
        for p in points:
            smallest_distance = float('Inf')
            idx = 0
            for i in range(k):
                distance = euclidean(p, clusters[i].center)
                if distance < smallest_distance:
                    smallest_distance = distance
                    idx = i
            plists[idx].append(p)
        diff = 0
        for i in range(k):
            old = clusters[i]
            center = calculate_center(plists[i], old.n)
            clusters[i] = Cluster(plists[i], center, old.n)
            diff = max(diff, euclidean(old.center, clusters[i].center))
        if diff < min_diff: break
    return clusters