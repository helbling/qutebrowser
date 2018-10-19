# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2015-2018 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Base class for a QtWebKit/QtWebEngine web inspector."""

import base64
import binascii

from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import pyqtSignal

from qutebrowser.config import configfiles
from qutebrowser.utils import log, usertypes
from qutebrowser.misc import miscwidgets, objects


def create(position, parent=None):
    """Get a WebKitInspector/WebEngineInspector.

    Args:
        position: position of the inspector (right/left/top/bottom/window).
        parent: The Qt parent to set.
    """
    # Importing modules here so we don't depend on QtWebEngine without the
    # argument and to avoid circular imports.
    if objects.backend == usertypes.Backend.QtWebEngine:
        from qutebrowser.browser.webengine import webengineinspector
        return webengineinspector.WebEngineInspector(position, parent)
    else:
        from qutebrowser.browser.webkit import webkitinspector
        return webkitinspector.WebKitInspector(position, parent)


class WebInspectorError(Exception):

    """Raised when the inspector could not be initialized."""

    pass


class AbstractWebInspector(QWidget):

    """A customized WebInspector which stores its geometry.

    Attributes:
        position: position of the inspector (right/left/top/bottom/window)

    Signals:
        closed: Emitted when the inspector is closed.
    """

    position = None
    closed = pyqtSignal()

    def __init__(self, position, parent=None):
        super().__init__(parent)
        self.position = position
        self._widget = None
        self._layout = miscwidgets.WrapperLayout(self)
        if position == 'window':
            self._load_state_geometry()

    def _set_widget(self, widget):
        self._widget = widget
        self._layout.wrap(self, widget)

    def detach(self):
        self.hide()
        self.setParent(None)
        self.position = 'window'
        self._load_state_geometry()
        self.show()

    def _load_state_geometry(self):
        """Load the geometry from the state file."""
        try:
            data = configfiles.state['geometry']['inspector']
            geom = base64.b64decode(data, validate=True)
        except KeyError:
            # First start
            pass
        except binascii.Error:
            log.misc.exception("Error while reading geometry")
        else:
            log.init.debug("Loading geometry from {}".format(geom))
            ok = self.restoreGeometry(geom)
            if not ok:
                log.init.warning("Error while loading geometry.")

    def closeEvent(self, e):
        """Save the window geometry when closed."""

        if self.position == 'window':
            data = bytes(self.saveGeometry())
            geom = base64.b64encode(data).decode('ASCII')
            configfiles.state['geometry']['inspector'] = geom

        super().closeEvent(e)
        self.closed.emit()

    def inspect(self, page):
        """Inspect the given QWeb(Engine)Page."""
        raise NotImplementedError
