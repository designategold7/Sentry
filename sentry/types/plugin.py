from sentry.types import SlottedModel

class PluginConfig(SlottedModel):
    def load(self, obj, *args, **kwargs):
        if not obj: obj = {}
        # Pre-filter fields and drop any marked private before sending up the Model chain
        obj_filtered = {
            k: v for k, v in obj.items()
            if k in self._fields and not getattr(self._fields[k], 'private', False)
        }
        return super(PluginConfig, self).load(obj_filtered, *args, **kwargs)