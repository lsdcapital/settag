"""SetTag's plain command-line interface.

Split across ``args`` (grammar), ``commands`` (dispatch and the work each
command performs), and ``render`` (terminal output). The submodule is named
``commands`` rather than ``main`` because re-exporting the ``main`` function
here would shadow a submodule of that name. The package keeps the original
module name, so ``settag.cli:main`` and existing imports still resolve.
"""

from settag.cli.commands import _analyze_one, main

__all__ = ["_analyze_one", "main"]
