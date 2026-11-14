"""Installed in $HERMES_HOME/scripts; no model call, no channel webhook."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ['HERMES_HOME']) / 'plugins/hermes-todo'))
from personal_notify import main
main()
