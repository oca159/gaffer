# -*- coding: utf-8 -
#
# This file is part of gaffer. See the NOTICE for more information.
"""
usage: gafferd [--version] [-v|-vv] [-c CONFIG|--config=CONFIG]
               [-p PLUGINS_DIR|--plugin-dir=PLUGINS_DIR]
               [--daemon] [--pidfile=PIDFILE]
               [--bind=ADDRESS] [--lookupd-address=LOOKUP]...
               [--broadcast-address=ADDR]
               [--certfile=CERTFILE] [--keyfile=KEYFILE]
               [--cacert=CACERT]
               [--client-certfile=CERTFILE] [--client-keyfile=KEYFILE]
               [--backlog=BACKLOG]
               [--error-log=FILE] [--log-level=LEVEL]

Args

    CONFIG                    configuration file path

Options

    -h --help                   show this help message and exit
    --version                   show version and exit
    -v -vv                      verbose mode
    -c CONFIG --config=CONFIG   configuration dir
    -p DIR --plugin-dir=DIR     plugin dir
    --daemon                    Start gaffer in daemon mode
    --pidfile=PIDFILE
    --bind=ADDRESS              default HTTP binding (default: 0.0.0.0:5000)
    --lookupd-address=LOOKUP    lookupd HTTP address
    --broadcast-address=ADDR    the address for this node. This is registered
                                with gaffer_lookupd (defaults to OS hostname)
    --broadcast-port=PORT       The port that will be registered with
                                gaffer_lookupd (defaults to local port)
    --certfile=CERTFILE         SSL certificate file for the default binding
    --keyfile=KEYFILE           SSL key file
    --client-certfile=CERTFILE  SSL client certificate file (to connect to the
                                lookup server)
    --client-keyfile=KEYFILE    SSL client key file
    --cacert=CACERT             SSL CA certificate
    --backlog=BACKLOG           default backlog (default: 128).
    --error-log=FILE            logging file
    --log-level=LEVEL           logging level (critical, error warning, info,
                                debug)

"""



import os
import logging
import sys


from .. import __version__
from ..console_output import ConsoleOutput
from ..docopt import docopt
from ..error import ProcessError
from ..manager import Manager
from ..pidfile import Pidfile
from ..process import ProcessConfig
from ..sig_handler import SigHandler
from ..util import daemonize, setproctitle_
from ..webhooks import WebHooks
from .config import ConfigError, Config
from .http import HttpHandler
from .plugins import PluginManager
from .util import user_path, system_path, default_path, is_admin


LOG_LEVELS = {
        "critical": logging.CRITICAL,
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "debug": logging.DEBUG}

LOG_ERROR_FMT = r"%(asctime)s [%(process)d] [%(levelname)s] %(message)s"
LOG_DATEFMT = r"%Y-%m-%d %H:%M:%S"


class Server(object):
    """ Server object used for gafferd """

    def __init__(self, args):
        self.args = args

        # get config dir
        self.config_dir = self.find_configdir()
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir)
        elif not os.path.isdir(self.config_dir):
            raise RuntimeError("%r isn't a directory" % self.config_dir)

        self.cfg = Config(args, self.config_dir)
        self.plugins = []


    def start(self, loop, manager):
        # the server is responsible of launching plugins instead of the
        # manager so we can manage the configuratino change
        self.plugin_manager.start_apps(self.cfg, loop, manager)

    def stop(self):
        # stop all plugins apps
        self.plugin_manager.stop_apps()


    def restart(self):
        logging.info("reload config")
        try:
            self.do_restart()
        except Exception as e:
            logging.error('Uncaught exception when stopping a plugin',
                        exc_info=True)

    def do_restart(self):
        try:
            jobs_removed, webhooks_removed = self.cfg.reload()
        except ConfigError as e:
            # if on restart we fail to parse the config then just return and
            # do nothing
            logging.error("failed parsing config: %s" % str(e))
            logging.info("config not reloaded")
            return

        # remove jobs config from the manager
        for jobname, sessionid in jobs_removed:
            self.manager.unload(jobname, sessionid=sessionid)

        # load or update job configs
        for name, sessionid, cmd, params in self.cfg.processes:
            if "start" in params:
                # on restart we don't start the loaded jobs. They will be
                # handled later by gaffer so just remove this param
                params.pop("start")

            config = ProcessConfig(name, cmd, **params)

            # so we need to update a job config
            update = True
            try:
                self.manager.get("%s.%s" % (sessionid, name))
            except ProcessError:
                update = False

            if update:
                self.manager.update(config, sessionid=sessionid)
            else:
                self.manager.load(config, sessionid=sessionid)

        # unregister hooks
        for event, url in webhooks_removed:
            self.webhook_app.unregister(event, url)

        # restart plugins
        self.plugin_manager.restart_apps(self.cfg, self.manager.loop,
                self.manager)

    def run(self):
        # load config
        self.cfg.load()

        # do we need to daemonize the daemon
        if self.cfg.daemonize:
            daemonize()

        # fix the process name
        setproctitle_("gafferd")

        # setup the pidfile
        pidfile = None
        if self.cfg.pidfile:
            pidfile = Pidfile(self.cfg.pidfile)
            try:
                pidfile.create(os.getpid())
            except RuntimeError as e:
                print(str(e))
                sys.exit(1)

        # initialize the plugin manager
        self.plugin_manager = PluginManager(self.cfg.plugin_dir)

        # check if any plugin dependancy is missing
        self.plugin_manager.check_mandatory()

        # initialize the manager
        self.manager = Manager()

        # initialize apps
        self.http_handler = HttpHandler(self.cfg, self.plugin_manager)
        self.webhook_app = WebHooks(hooks=self.cfg.webhooks)

        # setup gaffer apps
        apps = [self,
                SigHandler(),
                self.webhook_app,
                self.http_handler]

        # verbose mode
        if self.args["-v"] == 2:
            apps.append(ConsoleOutput(actions=['.']))
        elif self.args["-v"] == 1:
            apps.append(ConsoleOutput(output_streams=False))

        self.set_logging()

        # really start the server
        self.manager.start(apps=apps)

        # load job configs
        for name, sessionid, cmd, params in self.cfg.processes:
            if "start" in params:
                start = params.pop("start")
            else:
                start = True

            config = ProcessConfig(name, cmd, **params)
            self.manager.load(config, sessionid=sessionid, start=start)

        # run the main loop
        try:
            self.manager.run()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print("error: %s" % str(e))
            sys.exit(1)
        finally:
            if pidfile is not None:
                pidfile.unlink()

    def find_configdir(self):
        if self.args.get('--config') is not None:
            return self.args.get('--config')

        if 'GAFFER_CONFIG' in os.environ:
            return os.environ.get('GAFFER_CONFIG')

        if is_admin():
            default_paths = system_path()
        else:
            default_paths = user_path()

        for path in default_paths:
            if os.path.isdir(path):
                return path

        return default_path()

    def set_logging(self):
        logger = logging.getLogger()

        handlers = []
        if self.cfg.logfile is not None and self.cfg.logfile != "-":
            handlers.append(logging.FileHandler(self.cfg.logfile))
        else:
            handlers.append(logging.StreamHandler())

        loglevel = LOG_LEVELS.get(self.cfg.loglevel.lower(), logging.INFO)
        logger.setLevel(loglevel)

        format = r"%(asctime)s [%(process)d] [%(levelname)s] %(message)s"
        datefmt = r"%Y-%m-%d %H:%M:%S"
        for h in handlers:
            h.setFormatter(logging.Formatter(format, datefmt))
            logger.addHandler(h)

def run():
    args = docopt(__doc__, version=__version__)
    try:
        s = Server(args)
        s.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        print("error: %s" % str(e))
        sys.exit(1)

    sys.exit(0)

if __name__ == "__main__":
    run()
