# Author: echel0n <echel0n@sickrage.ca>
# URL: https://sickrage.ca
#
# This file is part of SickRage.
#
# SickRage is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SickRage is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SickRage.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import datetime
import os
import platform
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib
import urlparse
import uuid

import adba
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dateutil import tz
from fake_useragent import UserAgent
from tornado.ioloop import IOLoop

import sickrage
from sickrage.core.api import API
from sickrage.core.caches.name_cache import NameCache
from sickrage.core.common import SD, SKIPPED, WANTED
from sickrage.core.config import Config
from sickrage.core.databases.cache import CacheDB
from sickrage.core.databases.failed import FailedDB
from sickrage.core.databases.main import MainDB
from sickrage.core.google import GoogleAuth
from sickrage.core.helpers import findCertainShow, \
    generateCookieSecret, makeDir, get_lan_ip, restoreSR, getDiskSpaceUsage, getFreeSpace, launch_browser
from sickrage.core.helpers.encoding import get_sys_encoding, ek, patch_modules
from sickrage.core.logger import Logger
from sickrage.core.nameparser.validator import check_force_season_folders
from sickrage.core.processors import auto_postprocessor
from sickrage.core.processors.auto_postprocessor import AutoPostProcessor
from sickrage.core.queues.postprocessor import PostProcessorQueue
from sickrage.core.queues.search import SearchQueue
from sickrage.core.queues.show import ShowQueue
from sickrage.core.searchers.backlog_searcher import BacklogSearcher
from sickrage.core.searchers.daily_searcher import DailySearcher
from sickrage.core.searchers.proper_searcher import ProperSearcher
from sickrage.core.searchers.subtitle_searcher import SubtitleSearcher
from sickrage.core.searchers.trakt_searcher import TraktSearcher
from sickrage.core.tv.show import TVShow
from sickrage.core.ui import Notifications
from sickrage.core.updaters.show_updater import ShowUpdater
from sickrage.core.updaters.tz_updater import update_network_dict
from sickrage.core.version_updater import VersionUpdater
from sickrage.core.webserver import WebServer
from sickrage.core.websession import WebSession
from sickrage.metadata import MetadataProviders
from sickrage.notifiers import NotifierProviders
from sickrage.providers import SearchProviders


class Core(object):
    def __init__(self):
        self.started = False
        self.daemon = None
        self.io_loop = IOLoop().instance()
        self.pid = os.getpid()

        self.tz = tz.tzlocal()

        self.config_file = None
        self.data_dir = None
        self.cache_dir = None
        self.quite = None
        self.no_launch = None
        self.web_port = None
        self.developer = None
        self.debug = None
        self.newest_version = None
        self.newest_version_string = None

        self.user_agent = 'SiCKRAGE.CE.1/({};{};{})'.format(platform.system(), platform.release(), str(uuid.uuid1()))
        self.sys_encoding = get_sys_encoding()
        self.languages = [language for language in os.listdir(sickrage.LOCALE_DIR) if '_' in language]
        self.showlist = []

        self.api = None
        self.adba_connection = None
        self.notifier_providers = None
        self.metadata_providers = None
        self.search_providers = None
        self.log = None
        self.config = None
        self.alerts = None
        self.main_db = None
        self.cache_db = None
        self.failed_db = None
        self.scheduler = None
        self.wserver = None
        self.wsession = None
        self.google_auth = None
        self.name_cache = None
        self.show_queue = None
        self.search_queue = None
        self.postprocessor_queue = None
        self.version_updater = None
        self.show_updater = None
        self.daily_searcher = None
        self.backlog_searcher = None
        self.proper_searcher = None
        self.trakt_searcher = None
        self.subtitle_searcher = None
        self.auto_postprocessor = None

        # patch modules with encoding kludge
        patch_modules()

    def start(self):
        self.started = True

        # thread name
        threading.currentThread().setName('CORE')

        # init core classes
        self.notifier_providers = NotifierProviders()
        self.metadata_providers = MetadataProviders()
        self.search_providers = SearchProviders()
        self.log = Logger()
        self.config = Config()
        self.api = API()
        self.alerts = Notifications()
        self.main_db = MainDB()
        self.cache_db = CacheDB()
        self.failed_db = FailedDB()
        self.scheduler = BackgroundScheduler()
        self.wserver = WebServer()
        self.wsession = WebSession()
        self.google_auth = GoogleAuth()
        self.name_cache = NameCache()
        self.show_queue = ShowQueue()
        self.search_queue = SearchQueue()
        self.postprocessor_queue = PostProcessorQueue()
        self.version_updater = VersionUpdater()
        self.show_updater = ShowUpdater()
        self.daily_searcher = DailySearcher()
        self.backlog_searcher = BacklogSearcher()
        self.proper_searcher = ProperSearcher()
        self.trakt_searcher = TraktSearcher()
        self.subtitle_searcher = SubtitleSearcher()
        self.auto_postprocessor = AutoPostProcessor()


        # Check if we need to perform a restore first
        if os.path.exists(os.path.abspath(os.path.join(self.data_dir, 'restore'))):
            success = restoreSR(os.path.abspath(os.path.join(self.data_dir, 'restore')), self.data_dir)
            print("Restoring SiCKRAGE backup: %s!\n" % ("FAILED", "SUCCESSFUL")[success])
            if success:
                shutil.rmtree(os.path.abspath(os.path.join(self.data_dir, 'restore')), ignore_errors=True)

        # migrate old database file names to new ones
        if os.path.isfile(os.path.abspath(os.path.join(self.data_dir, 'sickbeard.db'))):
            if os.path.isfile(os.path.join(self.data_dir, 'sickrage.db')):
                helpers.moveFile(os.path.join(self.data_dir, 'sickrage.db'),
                                 os.path.join(self.data_dir, '{}.bak-{}'
                                              .format('sickrage.db',
                                                      datetime.datetime.now().strftime(
                                                          '%Y%m%d_%H%M%S'))))

            helpers.moveFile(os.path.abspath(os.path.join(self.data_dir, 'sickbeard.db')),
                             os.path.abspath(os.path.join(self.data_dir, 'sickrage.db')))

        # load config
        self.config.load()

        # set language
        self.config.change_gui_lang(self.config.gui_lang)

        # set socket timeout
        socket.setdefaulttimeout(self.config.socket_timeout)

        # setup logger settings
        self.log.logSize = self.config.log_size
        self.log.logNr = self.config.log_nr
        self.log.logFile = os.path.join(self.data_dir, 'logs', 'sickrage.log')
        self.log.debugLogging = self.config.debug
        self.log.consoleLogging = not self.quite

        # start logger
        self.log.start()

        # user agent
        if self.config.random_user_agent:
            self.user_agent = UserAgent().random

        urlparse.uses_netloc.append('scgi')
        urllib.FancyURLopener.version = self.user_agent

        # Check available space
        try:
            total_space, available_space = getFreeSpace(self.data_dir)
            if available_space < 100:
                self.log.error('Shutting down as SiCKRAGE needs some space to work. You\'ll get corrupted data '
                               'otherwise. Only %sMB left', available_space)
                return
        except Exception:
            self.log.error('Failed getting diskspace: %s', traceback.format_exc())

        # perform database startup actions
        for db in [self.main_db, self.cache_db, self.failed_db]:
            # initialize database
            db.initialize()

            # check integrity of database
            db.check_integrity()

            # migrate database
            db.migrate()

            # misc database cleanups
            db.cleanup()

        # compact main database
        if not sickrage.app.developer and self.config.last_db_compact < time.time() - 604800:  # 7 days
            self.main_db.compact()
            self.config.last_db_compact = int(time.time())

        # load name cache
        self.name_cache.load()

        # load data for shows from database
        self.load_shows()

        if self.config.default_page not in ('home', 'schedule', 'history', 'news', 'IRC'):
            self.config.default_page = 'home'

        # cleanup cache folder
        for folder in ['mako', 'sessions', 'indexers']:
            try:
                shutil.rmtree(os.path.join(sickrage.app.cache_dir, folder), ignore_errors=True)
            except Exception:
                continue

        # init anidb connection
        if self.config.use_anidb:
            def anidb_logger(msg):
                return self.log.debug("AniDB: {} ".format(msg))

            try:
                self.adba_connection = adba.Connection(keepAlive=True, log=anidb_logger)
                self.adba_connection.auth(self.config.anidb_username, self.config.anidb_password)
            except Exception as e:
                self.log.warning("AniDB exception msg: %r " % repr(e))

        if self.config.web_port < 21 or self.config.web_port > 65535:
            self.config.web_port = 8081

        if not self.config.web_cookie_secret:
            self.config.web_cookie_secret = generateCookieSecret()

        # attempt to help prevent users from breaking links by using a bad url
        if not self.config.anon_redirect.endswith('?'):
            self.config.anon_redirect = ''

        if not re.match(r'\d+\|[^|]+(?:\|[^|]+)*', self.config.root_dirs):
            self.config.root_dirs = ''

        self.config.naming_force_folders = check_force_season_folders()

        if self.config.nzb_method not in ('blackhole', 'sabnzbd', 'nzbget'):
            self.config.nzb_method = 'blackhole'

        if self.config.torrent_method not in ('blackhole', 'utorrent', 'transmission', 'deluge', 'deluged',
                                              'download_station', 'rtorrent', 'qbittorrent', 'mlnet', 'putio'):
            self.config.torrent_method = 'blackhole'

        if self.config.autopostprocessor_freq < self.config.min_autopostprocessor_freq:
            self.config.autopostprocessor_freq = self.config.min_autopostprocessor_freq
        if self.config.daily_searcher_freq < self.config.min_daily_searcher_freq:
            self.config.daily_searcher_freq = self.config.min_daily_searcher_freq
        self.config.min_backlog_searcher_freq = self.backlog_searcher.get_backlog_cycle_time()
        if self.config.backlog_searcher_freq < self.config.min_backlog_searcher_freq:
            self.config.backlog_searcher_freq = self.config.min_backlog_searcher_freq
        if self.config.version_updater_freq < self.config.min_version_updater_freq:
            self.config.version_updater_freq = self.config.min_version_updater_freq
        if self.config.subtitle_searcher_freq < self.config.min_subtitle_searcher_freq:
            self.config.subtitle_searcher_freq = self.config.min_subtitle_searcher_freq
        if self.config.proper_searcher_interval not in ('15m', '45m', '90m', '4h', 'daily'):
            self.config.proper_searcher_interval = 'daily'
        if self.config.showupdate_hour < 0 or self.config.showupdate_hour > 23:
            self.config.showupdate_hour = 0
        if self.config.subtitles_languages[0] == '':
            self.config.subtitles_languages = []

        # add version checker job
        self.scheduler.add_job(
            self.version_updater.run,
            IntervalTrigger(
                hours=self.config.version_updater_freq
            ),
            name="VERSIONUPDATER",
            id="VERSIONUPDATER"
        )

        # add network timezones updater job
        self.scheduler.add_job(
            update_network_dict,
            IntervalTrigger(
                days=1
            ),
            name="TZUPDATER",
            id="TZUPDATER"
        )

        # add show updater job
        self.scheduler.add_job(
            self.show_updater.run,
            IntervalTrigger(
                days=1,
                start_date=datetime.datetime.now().replace(hour=self.config.showupdate_hour)
            ),
            name="SHOWUPDATER",
            id="SHOWUPDATER"
        )

        # add daily search job
        self.scheduler.add_job(
            self.daily_searcher.run,
            IntervalTrigger(
                minutes=self.config.daily_searcher_freq,
                start_date=datetime.datetime.now() + datetime.timedelta(minutes=4)
            ),
            name="DAILYSEARCHER",
            id="DAILYSEARCHER"
        )

        # add backlog search job
        self.scheduler.add_job(
            self.backlog_searcher.run,
            IntervalTrigger(
                minutes=self.config.backlog_searcher_freq,
                start_date=datetime.datetime.now() + datetime.timedelta(minutes=30)
            ),
            name="BACKLOG",
            id="BACKLOG"
        )

        # add auto-postprocessing job
        self.scheduler.add_job(
            self.auto_postprocessor.run,
            IntervalTrigger(
                minutes=self.config.autopostprocessor_freq
            ),
            name="POSTPROCESSOR",
            id="POSTPROCESSOR"
        )

        # add find proper job
        self.scheduler.add_job(
            self.proper_searcher.run,
            IntervalTrigger(
                minutes={'15m': 15, '45m': 45, '90m': 90, '4h': 4 * 60, 'daily': 24 * 60}[
                    self.config.proper_searcher_interval]
            ),
            name="PROPERSEARCHER",
            id="PROPERSEARCHER"
        )

        # add trakt.tv checker job
        self.scheduler.add_job(
            self.trakt_searcher.run,
            IntervalTrigger(
                hours=1
            ),
            name="TRAKTSEARCHER",
            id="TRAKTSEARCHER"
        )

        # add subtitles finder job
        self.scheduler.add_job(
            self.subtitle_searcher.run,
            IntervalTrigger(
                hours=self.config.subtitle_searcher_freq
            ),
            name="SUBTITLESEARCHER",
            id="SUBTITLESEARCHER"
        )

        # start scheduler service
        self.scheduler.start()

        # Pause/Resume PROPERSEARCHER job
        (self.scheduler.get_job('PROPERSEARCHER').pause,
         self.scheduler.get_job('PROPERSEARCHER').resume
         )[self.config.download_propers]()

        # Pause/Resume TRAKTSEARCHER job
        (self.scheduler.get_job('TRAKTSEARCHER').pause,
         self.scheduler.get_job('TRAKTSEARCHER').resume
         )[self.config.use_trakt]()

        # Pause/Resume SUBTITLESEARCHER job
        (self.scheduler.get_job('SUBTITLESEARCHER').pause,
         self.scheduler.get_job('SUBTITLESEARCHER').resume
         )[self.config.use_subtitles]()

        # Pause/Resume POSTPROCESS job
        (self.scheduler.get_job('POSTPROCESSOR').pause,
         self.scheduler.get_job('POSTPROCESSOR').resume
         )[self.config.process_automatically]()

        # start queue's
        self.search_queue.start()
        self.show_queue.start()
        self.postprocessor_queue.start()

        # start webserver
        self.wserver.start()

    def shutdown(self, restart=False):
        if self.started:
            self.log.info('SiCKRAGE IS SHUTTING DOWN!!!')

            # shutdown webserver
            self.wserver.shutdown()

            # shutdown show queue
            if self.show_queue:
                self.log.debug("Shutting down show queue")
                self.show_queue.shutdown()
                del self.show_queue

            # shutdown search queue
            if self.search_queue:
                self.log.debug("Shutting down search queue")
                self.search_queue.shutdown()
                del self.search_queue

            # shutdown post-processor queue
            if self.postprocessor_queue:
                self.log.debug("Shutting down post-processor queue")
                self.postprocessor_queue.shutdown()
                del self.postprocessor_queue

            # log out of ADBA
            if self.adba_connection:
                self.log.debug("Shutting down ANIDB connection")
                self.adba_connection.stop()

            # save all show and config settings
            self.save_all()

            # close databases
            for db in [self.main_db, self.cache_db, self.failed_db]:
                if db.opened:
                    self.log.debug("Shutting down {} database connection".format(db.name))
                    db.close()

            # shutdown logging
            self.log.close()

        if restart:
            os.execl(sys.executable, sys.executable, *sys.argv)

        if sickrage.app.daemon:
            sickrage.app.daemon.stop()

        self.started = False

    def save_all(self):
        # write all shows
        self.log.info("Saving all shows to the database")
        for show in self.showlist:
            try:
                show.saveToDB()
            except Exception:
                continue

        # save config
        self.config.save()

    def load_shows(self):
        """
        Populates the showlist with shows from the database
        """

        for dbData in [x['doc'] for x in self.main_db.db.all('tv_shows', with_doc=True)]:
            try:
                self.log.debug("Loading data for show: [{}]".format(dbData['show_name']))
                show = TVShow(int(dbData['indexer']), int(dbData['indexer_id']))
                show.nextEpisode()
                self.showlist += [show]
            except Exception as e:
                self.log.error("Show error in [%s]: %s" % (dbData['location'], e.message))