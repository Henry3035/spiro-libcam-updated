# logger.py -
#   simple functions for logging
#

import sys
from spiro.config import Config

cfg = Config()

def log(msg):
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()

def debug(msg, exc_info=False):
    if cfg.get('debug'):
        sys.stderr.write(msg + '\n')
        if exc_info:
            import traceback
            sys.stderr.write(traceback.format_exc() + '\n')
        sys.stderr.flush()
