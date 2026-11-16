"""Framework providers for the route pass. See `provider.py` for how to add one.

`REGISTRY` lists every provider; `detect.select_providers` picks the ones a repository's root
`composer.json` requires directly. Selected providers never share roles: each one's guards and
grant calls apply only to the routes it produced.
"""

from __future__ import annotations

from .codeigniter import PROVIDER as CODEIGNITER
from .laravel import PROVIDER as LARAVEL
from .symfony import PROVIDER as SYMFONY

REGISTRY = (SYMFONY, LARAVEL, CODEIGNITER)

__all__ = ["CODEIGNITER", "LARAVEL", "REGISTRY", "SYMFONY"]
