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
#  GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SickRage.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import datetime
import threading
import time

from CodernityDB.database import RecordNotFound

import sickrage
from sickrage.core.exceptions import CantRefreshShowException, CantUpdateShowException
from sickrage.core.ui import ProgressIndicators, QueueProgressIndicator
from sickrage.indexers import IndexerApi


class ShowUpdater(object):
    def __init__(self):
        self.name = "SHOWUPDATER"
        self.lock = threading.Lock()
        self.amActive = False

    def run(self, force=False):
        if self.amActive:
            return

        self.amActive = True

        # set thread name
        threading.currentThread().setName(self.name)

        update_timestamp = int(time.mktime(datetime.datetime.now().timetuple()))

        try:
            dbData = sickrage.app.cache_db.db.get('lastUpdate', 'theTVDB', with_doc=True)['doc']
            last_update = int(dbData['time'])
        except RecordNotFound:
            last_update = update_timestamp
            dbData = sickrage.app.cache_db.db.insert({
                '_t': 'lastUpdate',
                'provider': 'theTVDB',
                'time': 0
            })

        # get indexer updated show ids
        indexer_api = IndexerApi().indexer(**IndexerApi().api_params.copy())
        updated_shows = set(s["id"] for s in indexer_api.updated(last_update) or {})

        # start update process
        pi_list = []
        for show in sickrage.app.showlist:
            if show.paused:
                sickrage.app.log.info('Show update skipped, show: {} is paused.'.format(show.name))
            try:
                show.nextEpisode()
                stale = (datetime.datetime.now() - datetime.datetime.fromordinal(show.last_update)).days > 7
                if show.indexerid in updated_shows or stale:
                    pi_list.append(sickrage.app.show_queue.updateShow(show, False))
                else:
                    pi_list.append(sickrage.app.show_queue.refreshShow(show, False))
            except (CantUpdateShowException, CantRefreshShowException) as e:
                sickrage.app.log.debug("Automatic update failed: {}".format(e.message))

        ProgressIndicators.setIndicator('dailyShowUpdates', QueueProgressIndicator("Daily Show Updates", pi_list))

        dbData['time'] = update_timestamp
        sickrage.app.cache_db.db.update(dbData)

        self.amActive = False