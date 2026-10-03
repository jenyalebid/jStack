import sys

from .. import install_host
from .cli import main

install_host.adopt_installed_environment(install_host.plist_path())
sys.exit(main())
