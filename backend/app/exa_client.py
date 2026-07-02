"""
Tracked Exa client — drop-in for exa_py.Exa that logs one usage row per search.
Modules change their import:
    from app.exa_client import Exa     # was: from exa_py import Exa
Everything else (Exa(api_key=...), exa.search(...)) is unchanged.
"""

from exa_py import Exa as _Exa
from app import usage


class Exa(_Exa):
    def search(self, *args, **kwargs):
        result = super().search(*args, **kwargs)
        try:
            usage.log_exa()
        except Exception:
            pass
        return result
