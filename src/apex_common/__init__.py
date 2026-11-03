"""Infrastructure shared by the apex services.

Import from the modules, not from here — `from apex_common.title_norm import
normalize_title`, not `from apex_common import normalize_title`. A flat
re-export surface would make every consumer import pydantic and the logging
machinery to use one regex, and it hides which module owns a name.
"""

__version__ = "0.2.0"
