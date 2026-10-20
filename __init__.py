"""PairDown agent half. It has nothing to register yet.

The package ships a Desktop pane (desktop/plugin.js) and backend routes
(dashboard/plugin_api.py), and Hermes finds both from their own files. Hermes
still imports this module when the plugin is loaded and calls register(), so the
function has to exist.
"""


def register(ctx):
    """Register nothing: the pane and the routes are declared by the files beside this one."""
