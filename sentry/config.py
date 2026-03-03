import yaml
with open('config.yaml', 'r') as f:
    # Security patch: Upgraded from yaml.load to yaml.safe_load
    loaded = yaml.safe_load(f.read())
    locals().update(loaded)