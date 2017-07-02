# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2017 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
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

"""Types for options in qutebrowser's configuration.

Those types are used in configdata.yml as type of a setting.

Most of them are pretty generic, but some of them are e.g. specific String
subclasses with valid_values set, as that particular "type" is used multiple
times in the config.

A setting value can be represented in three different ways:

1) As an object which can be represented in YAML:
   str, list, dict, int, float, True/False/None
   This is what qutebrowser actually saves internally, and also what it gets
   from the YAML or config.py.
2) As a string. This is e.g. used by the :set command.
3) As the value the code which uses it expects, e.g. enum members.

Config types can do different conversations:

- Object to string with .to_str() (1 -> 2)
- String to object with .from_str() (2 -> 1)
- Object to code with .to_py() (1 -> 3)
  This also validates whether the object is actually correct (type/value).
"""

import re
import shlex
import codecs
import os.path
import itertools
import collections
import warnings
import datetime
import functools
import operator
import json

import yaml
from PyQt5.QtCore import QUrl, Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import QTabWidget, QTabBar

from qutebrowser.commands import cmdutils
from qutebrowser.config import configexc
from qutebrowser.utils import standarddir, utils, qtutils


SYSTEM_PROXY = object()  # Return value for Proxy type

# Taken from configparser
BOOLEAN_STATES = {'1': True, 'yes': True, 'true': True, 'on': True,
                  '0': False, 'no': False, 'false': False, 'off': False}


class ValidValues:

    """Container for valid values for a given type.

    Attributes:
        values: A list with the allowed untransformed values.
        descriptions: A dict with value/desc mappings.
    """

    def __init__(self, *values):
        if not values:
            raise ValueError("ValidValues with no values makes no sense!")
        self.descriptions = {}
        self.values = []
        for value in values:
            if isinstance(value, str):
                # Value without description
                self.values.append(value)
            elif isinstance(value, dict):
                # List of dicts from configdata.yml
                assert len(value) == 1, value
                value, desc = list(value.items())[0]
                self.values.append(value)
                self.descriptions[value] = desc
            else:
                # (value, description) tuple
                self.values.append(value[0])
                self.descriptions[value[0]] = value[1]

    def __contains__(self, val):
        return val in self.values

    def __iter__(self):
        return self.values.__iter__()

    def __repr__(self):
        return utils.get_repr(self, values=self.values,
                              descriptions=self.descriptions)

    def __eq__(self, other):
        return (self.values == other.values and
                self.descriptions == other.descriptions)


class BaseType:

    """A type used for a setting value.

    Attributes:
        none_ok: Whether to convert to None for an empty string.

    Class attributes:
        valid_values: Possible values if they can be expressed as a fixed
                      string. ValidValues instance.
    """

    def __init__(self, none_ok=False):
        self.none_ok = none_ok
        self.valid_values = None

    def get_name(self):
        """Get a name for the type for documentation."""
        return self.__class__.__name__

    def get_valid_values(self):
        """Get the type's valid values for documentation."""
        return self.valid_values

    def _basic_py_validation(self, value, pytype):
        """Do some basic validation for Python values (emptyness, type).

        Arguments:
            value: The value to check.
            pytype: A Python type to check the value against.
        """
        if value is None:
            if not self.none_ok:
                raise configexc.ValidationError(value, "may not be null!")
            else:
                return

        if (not isinstance(value, pytype) or
                pytype is int and isinstance(value, bool)):
            if isinstance(pytype, tuple):
                expected = ' or '.join(typ.__name__ for typ in pytype)
            else:
                expected = pytype.__name__
            raise configexc.ValidationError(
                value, "expected a value of type {} but got {}.".format(
                    expected, type(value).__name__))

        if isinstance(value, str):
            self._basic_str_validation(value)

    def _basic_str_validation(self, value):
        """Do some basic validation for string values (empty, non-printable chars).

        Arguments:
            value: The value to check.
        """
        assert isinstance(value, str)
        if not value and not self.none_ok:
            raise configexc.ValidationError(value, "may not be empty!")
        if any(ord(c) < 32 or ord(c) == 0x7f for c in value):
            raise configexc.ValidationError(
                value, "may not contain unprintable chars!")

    def _validate_valid_values(self, value):
        """Validate value against possible values.

        The default implementation checks the value against self.valid_values
        if it was defined.

        Args:
            value: The value to validate.
        """
        if self.valid_values is not None:
            if value not in self.valid_values:
                raise configexc.ValidationError(
                    value,
                    "valid values: {}".format(', '.join(self.valid_values)))

    def from_str(self, value):
        """Get the setting value from a string.

        By default this invokes to_py() for validation and returns the unaltered
        value. This means that if to_py() returns a string rather than something
        more sophisticated, this doesn't need to be implemented.

        Args:
            value: The original string value.

        Return:
            The transformed value.
        """
        self._basic_str_validation(value)
        self.to_py(value)  # for validation
        if not value:
            return None
        return value

    def to_py(self, value):
        """Get the setting value from a Python value.

        Args:
            value: The value we got from Python/YAML.

        Return:
            The transformed value.

        Raise:
            configexc.ValidationError if the value was invalid.
        """
        raise NotImplementedError

    def to_str(self, value):
        """Get a string from the setting value.

        The resulting string should be parseable again by from_str.
        """
        if value is None:
            return ''
        assert isinstance(value, str)
        return value

    def complete(self):
        """Return a list of possible values for completion.

        The default implementation just returns valid_values, but it might be
        useful to override this for special cases.

        Return:
            A list of (value, description) tuples or None.
        """
        if self.valid_values is None:
            return None
        else:
            out = []
            for val in self.valid_values:
                try:
                    desc = self.valid_values.descriptions[val]
                except KeyError:
                    # Some values are self-explaining and don't need a
                    # description.
                    desc = ""
                out.append((val, desc))
            return out


class MappingType(BaseType):

    """Base class for any setting which has a mapping to the given values.

    Attributes:
        MAPPING: The mapping to use.
    """

    MAPPING = {}

    def __init__(self, none_ok=False, valid_values=None):
        super().__init__(none_ok)
        self.valid_values = valid_values

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None
        self._validate_valid_values(value.lower())
        return self.MAPPING[value.lower()]


class String(BaseType):

    """Base class for a string setting (case-insensitive).

    Attributes:
        minlen: Minimum length (inclusive).
        maxlen: Maximum length (inclusive).
        forbidden: Forbidden chars in the string.
        completions: completions to be used, or None
    """

    def __init__(self, *, minlen=None, maxlen=None, forbidden=None,
                 encoding=None, none_ok=False, completions=None,
                 valid_values=None):
        super().__init__(none_ok)
        self.valid_values = valid_values

        if minlen is not None and minlen < 1:
            raise ValueError("minlen ({}) needs to be >= 1!".format(minlen))
        elif maxlen is not None and maxlen < 1:
            raise ValueError("maxlen ({}) needs to be >= 1!".format(maxlen))
        elif maxlen is not None and minlen is not None and maxlen < minlen:
            raise ValueError("minlen ({}) needs to be <= maxlen ({})!".format(
                minlen, maxlen))
        self.minlen = minlen
        self.maxlen = maxlen
        self.forbidden = forbidden
        self._completions = completions
        self.encoding = encoding

    def _validate_encoding(self, value):
        """Check if the given value fits into the configured encoding.

        Raises ValidationError if not.

        Args:
            value: The value to check.
        """
        if self.encoding is None:
            return

        try:
            value.encode(self.encoding)
        except UnicodeEncodeError as e:
            msg = "{!r} contains non-{} characters: {}".format(
                value, self.encoding, e)
            raise configexc.ValidationError(value, msg)

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        self._validate_encoding(value)
        self._validate_valid_values(value)

        if self.forbidden is not None and any(c in value
                                              for c in self.forbidden):
            raise configexc.ValidationError(value, "may not contain the chars "
                                            "'{}'".format(self.forbidden))
        if self.minlen is not None and len(value) < self.minlen:
            raise configexc.ValidationError(value, "must be at least {} chars "
                                            "long!".format(self.minlen))
        if self.maxlen is not None and len(value) > self.maxlen:
            raise configexc.ValidationError(value, "must be at most {} chars "
                                            "long!".format(self.maxlen))

        return value

    def complete(self):
        if self._completions is not None:
            return self._completions
        else:
            return super().complete()


class UniqueCharString(String):

    """A string which may not contain duplicate chars."""

    def to_py(self, value):
        value = super().to_py(value)
        if not value:
            return None

        # Check for duplicate values
        if len(set(value)) != len(value):
            raise configexc.ValidationError(
                value, "String contains duplicate values!")

        return value


class List(BaseType):

    """Base class for a (string-)list setting."""

    _show_valtype = True

    def __init__(self, valtype, none_ok=False, length=None):
        super().__init__(none_ok)
        self.valtype = valtype
        self.length = length

    def get_name(self):
        name = super().get_name()
        if self._show_valtype:
            name += " of " + self.valtype.get_name()
        return name

    def get_valid_values(self):
        return self.valtype.get_valid_values()

    def from_str(self, value):
        self._basic_str_validation(value)
        if not value:
            return None

        try:
            yaml_val = utils.yaml_load(value)
        except yaml.YAMLError as e:
            raise configexc.ValidationError(value, str(e))

        # For the values, we actually want to call to_py, as we did parse them
        # from YAML, so they are numbers/booleans/... already.
        self.to_py(yaml_val)
        return yaml_val

    def to_py(self, value):
        self._basic_py_validation(value, list)
        if not value:
            return None

        if self.length is not None and len(value) != self.length:
            raise configexc.ValidationError(value, "Exactly {} values need to "
                                            "be set!".format(self.length))
        return [self.valtype.to_py(v) for v in value]

    def to_str(self, value):
        if not value:
            # An empty list is treated just like None -> empty string
            return ''
        return json.dumps(value)


class FlagList(List):

    """Base class for a list setting that contains one or more flags.

    Lists with duplicate flags are invalid and each item is checked against
    self.valid_values (if not empty).
    """

    combinable_values = None

    _show_valtype = False

    def __init__(self, none_ok=False, valid_values=None, length=None):
        super().__init__(valtype=String(), none_ok=none_ok, length=length)
        self.valtype.valid_values = valid_values

    def _check_duplicates(self, values):
        if len(set(values)) != len(values):
            raise configexc.ValidationError(
                values, "List contains duplicate values!")

    def to_py(self, value):
        vals = super().to_py(value)
        if vals is not None:
            self._check_duplicates(vals)
        return vals

    def complete(self):
        valid_values = self.valtype.valid_values
        if valid_values is None:
            return None

        out = []
        # Single value completions
        for value in valid_values:
            desc = valid_values.descriptions.get(value, "")
            out.append((value, desc))

        combinables = self.combinable_values
        if combinables is None:
            combinables = list(valid_values)
        # Generate combinations of each possible value combination
        for size in range(2, len(combinables) + 1):
            for combination in itertools.combinations(combinables, size):
                out.append((','.join(combination), ''))
        return out


class Bool(BaseType):

    """Base class for a boolean setting."""

    def __init__(self, none_ok=False):
        super().__init__(none_ok)
        self.valid_values = ValidValues('true', 'false')

    def to_py(self, value):
        self._basic_py_validation(value, bool)
        return value

    def from_str(self, value):
        self._basic_str_validation(value)
        if not value:
            return None

        try:
            return BOOLEAN_STATES[value.lower()]
        except KeyError:
            raise configexc.ValidationError(value, "must be a boolean!")

    def to_str(self, value):
        mapping = {
            None: '',
            True: 'true',
            False: 'false',
        }
        return mapping[value]


class BoolAsk(Bool):

    """A yes/no/ask question."""

    def __init__(self, none_ok=False):
        super().__init__(none_ok)
        self.valid_values = ValidValues('true', 'false', 'ask')

    def to_py(self, value):
        # basic validation unneeded if it's == 'ask' and done by Bool if we
        # call super().to_py
        if isinstance(value, str) and value.lower() == 'ask':
            return 'ask'
        return super().to_py(value)

    def from_str(self, value):
        # basic validation unneeded if it's == 'ask' and done by Bool if we
        # call super().from_str
        if isinstance(value, str) and value.lower() == 'ask':
            return 'ask'
        return super().from_str(value)

    def to_str(self, value):
        mapping = {
            None: '',
            True: 'true',
            False: 'false',
            'ask': 'ask',
        }
        return mapping[value]


class _Numeric(BaseType):  # pylint: disable=abstract-method

    """Base class for Float/Int.

    Attributes:
        minval: Minimum value (inclusive).
        maxval: Maximum value (inclusive).
    """

    def __init__(self, minval=None, maxval=None, none_ok=False):
        super().__init__(none_ok)
        self.minval = self._parse_bound(minval)
        self.maxval = self._parse_bound(maxval)
        if self.maxval is not None and self.minval is not None:
            if self.maxval < self.minval:
                raise ValueError("minval ({}) needs to be <= maxval ({})!"
                                 .format(self.minval, self.maxval))

    def _parse_bound(self, bound):
        """Get a numeric bound from a string like 'maxint'."""
        if bound == 'maxint':
            return qtutils.MAXVALS['int']
        elif bound == 'maxint64':
            return qtutils.MAXVALS['int64']
        else:
            if bound is not None:
                assert isinstance(bound, (int, float)), bound
            return bound

    def _validate_bounds(self, value, suffix=''):
        """Validate self.minval and self.maxval."""
        if value is None:
            return
        elif self.minval is not None and value < self.minval:
            raise configexc.ValidationError(
                value, "must be {}{} or bigger!".format(self.minval, suffix))
        elif self.maxval is not None and value > self.maxval:
            raise configexc.ValidationError(
                value, "must be {}{} or smaller!".format(self.maxval, suffix))

    def to_str(self, value):
        if value is None:
            return ''
        return str(value)


class Int(_Numeric):

    """Base class for an integer setting."""

    def from_str(self, value):
        self._basic_str_validation(value)
        if not value:
            return None

        try:
            intval = int(value)
        except ValueError:
            raise configexc.ValidationError(value, "must be an integer!")
        self.to_py(intval)
        return intval

    def to_py(self, value):
        self._basic_py_validation(value, int)
        self._validate_bounds(value)
        return value


class Float(_Numeric):

    """Base class for a float setting."""

    def from_str(self, value):
        self._basic_str_validation(value)
        if not value:
            return None

        try:
            floatval = float(value)
        except ValueError:
            raise configexc.ValidationError(value, "must be a float!")
        self.to_py(floatval)
        return floatval

    def to_py(self, value):
        self._basic_py_validation(value, (int, float))
        self._validate_bounds(value)
        return value


class Perc(_Numeric):

    """A percentage, as a string ending with %."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        if not value.endswith('%'):
            raise configexc.ValidationError(value, "does not end with %")
        try:
            floatval = float(value[:-1])
        except ValueError:
            raise configexc.ValidationError(value, "must be a percentage!")
        self._validate_bounds(floatval, suffix='%')
        return floatval

    def to_str(self, value):
        if value is None:
            return ''
        return value


class PercOrInt(_Numeric):

    """Percentage or integer.

    Attributes:
        minperc: Minimum value for percentage (inclusive).
        maxperc: Maximum value for percentage (inclusive).
        minint: Minimum value for integer (inclusive).
        maxint: Maximum value for integer (inclusive).
    """

    def __init__(self, minperc=None, maxperc=None, minint=None, maxint=None,
                 none_ok=False):
        super().__init__(minval=minint, maxval=maxint, none_ok=none_ok)
        self.minperc = self._parse_bound(minperc)
        self.maxperc = self._parse_bound(maxperc)
        if (self.maxperc is not None and self.minperc is not None and
                self.maxperc < self.minperc):
            raise ValueError("minperc ({}) needs to be <= maxperc "
                             "({})!".format(self.minperc, self.maxperc))

    def from_str(self, value):
        self._basic_str_validation(value)
        if not value:
            return None

        if value.endswith('%'):
            self.to_py(value)
            return value

        try:
            intval = int(value)
        except ValueError:
            raise configexc.ValidationError(value,
                                            "must be integer or percentage!")
        self.to_py(intval)
        return intval

    def to_py(self, value):
        """Expect a value like '42%' as string, or 23 as int."""
        self._basic_py_validation(value, (int, str))
        if value is None:
            return None

        if isinstance(value, str):
            if not value.endswith('%'):
                raise configexc.ValidationError(value, "does not end with %")

            try:
                intval = int(value[:-1])
            except ValueError:
                raise configexc.ValidationError(value, "invalid percentage!")

            if self.minperc is not None and intval < self.minperc:
                raise configexc.ValidationError(value, "must be {}% or "
                                                "more!".format(self.minperc))
            if self.maxperc is not None and intval > self.maxperc:
                raise configexc.ValidationError(value, "must be {}% or "
                                                "less!".format(self.maxperc))

            # Note we don't actually return the integer here, as we need to
            # know whether it was a percentage.
        else:
            self._validate_bounds(value)
        return value


class Command(BaseType):

    """Base class for a command value with arguments."""

    def to_py(self, value):
        # FIXME:conf require a list here?
        self._basic_py_validation(value, str)
        if not value:
            return
        split = value.split()
        if not split or split[0] not in cmdutils.cmd_dict:
            raise configexc.ValidationError(value, "must be a valid command!")
        return value

    def complete(self):
        out = []
        for cmdname, obj in cmdutils.cmd_dict.items():
            out.append((cmdname, obj.desc))
        return out


class ColorSystem(MappingType):

    """Color systems for interpolation."""

    def __init__(self, none_ok=False):
        super().__init__(
            none_ok,
            valid_values=ValidValues(
                ('rgb', "Interpolate in the RGB color system."),
                ('hsv', "Interpolate in the HSV color system."),
                ('hsl', "Interpolate in the HSL color system."),
                ('none', "Don't show a gradient.")))

    MAPPING = {
        'rgb': QColor.Rgb,
        'hsv': QColor.Hsv,
        'hsl': QColor.Hsl,
        'none': None,
    }


class QtColor(BaseType):

    """Base class for QColor."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        color = QColor(value)
        if color.isValid():
            return color
        else:
            raise configexc.ValidationError(value, "must be a valid color")


class QssColor(BaseType):

    """Color used in a Qt stylesheet."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        functions = ['rgb', 'rgba', 'hsv', 'hsva', 'qlineargradient',
                     'qradialgradient', 'qconicalgradient']
        if (any(value.startswith(func + '(') for func in functions) and
                value.endswith(')')):
            # QColor doesn't handle these
            return value

        if not QColor.isValidColor(value):
            raise configexc.ValidationError(value, "must be a valid color")

        return value


class Font(BaseType):

    """Base class for a font value."""

    font_regex = re.compile(r"""
        ^(
            (
                # style
                (?P<style>normal|italic|oblique) |
                # weight (named | 100..900)
                (
                    (?P<weight>[123456789]00) |
                    (?P<namedweight>normal|bold)
                ) |
                # size (<float>pt | <int>px)
                (?P<size>[0-9]+((\.[0-9]+)?[pP][tT]|[pP][xX]))
            )\           # size/weight/style are space-separated
        )*               # 0-inf size/weight/style tags
        (?P<family>.+)$  # mandatory font family""", re.VERBOSE)

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        if not self.font_regex.match(value):  # pragma: no cover
            # This should never happen, as the regex always matches everything
            # as family.
            raise configexc.ValidationError(value, "must be a valid font")

        return value


class FontFamily(Font):

    """A Qt font family."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        match = self.font_regex.match(value)
        if not match:  # FIXME:conf this used to have "pragma: no cover"
            raise configexc.ValidationError(value, "must be a valid font")
        for group in 'style', 'weight', 'namedweight', 'size':
            if match.group(group):
                raise configexc.ValidationError(value, "may not include a "
                                                "{}!".format(group))

        return value


class QtFont(Font):

    """A Font which gets converted to a QFont."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        style_map = {
            'normal': QFont.StyleNormal,
            'italic': QFont.StyleItalic,
            'oblique': QFont.StyleOblique,
        }
        weight_map = {
            'normal': QFont.Normal,
            'bold': QFont.Bold,
        }
        font = QFont()
        font.setStyle(QFont.StyleNormal)
        font.setWeight(QFont.Normal)

        match = self.font_regex.match(value)
        if not match:  # FIXME:conf this used to have "pragma: no cover"
            raise configexc.ValidationError(value, "must be a valid font")

        style = match.group('style')
        weight = match.group('weight')
        namedweight = match.group('namedweight')
        size = match.group('size')
        family = match.group('family')
        if style:
            font.setStyle(style_map[style])
        if namedweight:
            font.setWeight(weight_map[namedweight])
        if weight:
            # based on qcssparser.cpp:setFontWeightFromValue
            font.setWeight(min(int(weight) / 8, 99))
        if size:
            if size.lower().endswith('pt'):
                font.setPointSizeF(float(size[:-2]))
            elif size.lower().endswith('px'):
                font.setPixelSize(int(size[:-2]))
            else:
                # This should never happen as the regex only lets pt/px
                # through.
                raise ValueError("Unexpected size unit in {!r}!".format(
                    size))  # pragma: no cover
        # The Qt CSS parser handles " and ' before passing the string to
        # QFont.setFamily. We could do proper CSS-like parsing here, but since
        # hopefully nobody will ever have a font with quotes in the family (if
        # that's even possible), we take a much more naive approach.
        family = family.replace('"', '').replace("'", '')
        font.setFamily(family)
        return font


class Regex(BaseType):

    """A regular expression.

    Attributes:
        flags: The flags to be used when a regex is passed.
        _regex_type: The Python type of a regex object.
    """

    def __init__(self, flags=0, none_ok=False):
        super().__init__(none_ok)
        self._regex_type = type(re.compile(''))
        # Parse flags from configdata.yml
        if flags == 0:
            self.flags = flags
        else:
            self.flags = functools.reduce(
                operator.or_,
                (getattr(re, flag.strip()) for flag in flags.split(' | ')))

    def _compile_regex(self, pattern):
        """Check if the given regex is valid.

        This is more complicated than it could be since there's a warning on
        invalid escapes with newer Python versions, and we want to catch that
        case and treat it as invalid.
        """
        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter('always')
            try:
                compiled = re.compile(pattern, self.flags)
            except re.error as e:
                raise configexc.ValidationError(
                    pattern, "must be a valid regex - " + str(e))
            except RuntimeError:  # pragma: no cover
                raise configexc.ValidationError(
                    pattern, "must be a valid regex - recursion depth "
                    "exceeded")

        for w in recorded_warnings:
            if (issubclass(w.category, DeprecationWarning) and
                    str(w.message).startswith('bad escape')):
                raise configexc.ValidationError(
                    pattern, "must be a valid regex - " + str(w.message))
            else:
                warnings.warn(w.message)

        return compiled

    def to_py(self, value):
        """Get a compiled regex from either a string or a regex object."""
        self._basic_py_validation(value, (str, self._regex_type))
        if not value:
            return None
        elif isinstance(value, str):
            return self._compile_regex(value)
        else:
            # FIXME:conf is it okay if we ignore flags here?
            return value

    def to_str(self, value):
        if value is None:
            return ''
        elif isinstance(value, self._regex_type):
            return value.pattern
        else:
            return value


class Dict(BaseType):

    """A dictionary of values."""

    def __init__(self, keytype, valtype, *, fixed_keys=None, none_ok=False):
        super().__init__(none_ok)
        # If the keytype is not a string, we'll get problems with showing it as
        # json in to_str() as json converts keys to strings.
        assert isinstance(keytype, String), keytype
        self.keytype = keytype
        self.valtype = valtype
        self.fixed_keys = fixed_keys

    def _validate_keys(self, value):
        if (self.fixed_keys is not None and
                value.keys() != set(self.fixed_keys)):
            raise configexc.ValidationError(
                value, "Expected keys {}".format(self.fixed_keys))

    def from_str(self, value):
        self._basic_str_validation(value)
        if not value:
            return None

        try:
            yaml_val = utils.yaml_load(value)
        except yaml.YAMLError as e:
            raise configexc.ValidationError(value, str(e))

        # For the values, we actually want to call to_py, as we did parse them
        # from YAML, so they are numbers/booleans/... already.
        self.to_py(yaml_val)
        return yaml_val

    def to_py(self, value):
        self._basic_py_validation(value, dict)
        if not value:
            return None

        self._validate_keys(value)

        return {self.keytype.to_py(key): self.valtype.to_py(val)
                for key, val in value.items()}

    def to_str(self, value):
        if not value:
            # An empty Dict is treated just like None -> empty string
            return ''
        return json.dumps(value)


class File(BaseType):

    """A file on the local filesystem."""

    def __init__(self, required=True, **kwargs):
        super().__init__(**kwargs)
        self.required = required

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        value = os.path.expanduser(value)
        value = os.path.expandvars(value)
        try:
            if not os.path.isabs(value):
                value = os.path.join(standarddir.config(), value)
                not_isfile_message = ("must be a valid path relative to the "
                                      "config directory!")
            else:
                not_isfile_message = "must be a valid file!"
            if self.required and not os.path.isfile(value):
                raise configexc.ValidationError(value, not_isfile_message)
        except UnicodeEncodeError as e:
            raise configexc.ValidationError(value, e)

        return value


class Directory(BaseType):

    """A directory on the local filesystem."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None
        value = os.path.expandvars(value)
        value = os.path.expanduser(value)
        try:
            if not os.path.isdir(value):
                raise configexc.ValidationError(
                    value, "must be a valid directory!")
            if not os.path.isabs(value):
                raise configexc.ValidationError(
                    value, "must be an absolute path!")
        except UnicodeEncodeError as e:
            raise configexc.ValidationError(value, e)

        return value


class FormatString(BaseType):

    """A string with '{foo}'-placeholders."""

    def __init__(self, fields, none_ok=False):
        super().__init__(none_ok)
        self.fields = fields

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        try:
            value.format(**{k: '' for k in self.fields})
        except (KeyError, IndexError) as e:
            raise configexc.ValidationError(value, "Invalid placeholder "
                                            "{}".format(e))
        except ValueError as e:
            raise configexc.ValidationError(value, str(e))

        return value


class ShellCommand(BaseType):

    """A shellcommand which is split via shlex.

    Attributes:
        placeholder: If there should be a placeholder.
    """

    def __init__(self, placeholder=False, none_ok=False):
        super().__init__(none_ok)
        self.placeholder = placeholder

    def from_str(self, value):
        self._basic_str_validation(value)
        if not value:
            return None
        try:
            split_val = shlex.split(value)
        except ValueError as e:
            raise configexc.ValidationError(value, str(e))

        self.to_py(split_val)
        return split_val

    def to_py(self, value):
        # FIXME:conf require a str/list here?
        self._basic_py_validation(value, list)
        if not value:
            return None

        if self.placeholder and '{}' not in ' '.join(value):
            raise configexc.ValidationError(value, "needs to contain a "
                                            "{}-placeholder.")
        return value


class Proxy(BaseType):

    """A proxy URL or special value."""

    def __init__(self, none_ok=False):
        super().__init__(none_ok)
        self.valid_values = ValidValues(
            ('system', "Use the system wide proxy."),
            ('none', "Don't use any proxy"))

    def to_py(self, value):
        from qutebrowser.utils import urlutils
        self._basic_py_validation(value, str)
        if not value:
            return None

        try:
            if value == 'system':
                return SYSTEM_PROXY

            if value == 'none':
                url = QUrl('direct://')
            else:
                # If we add a special value to valid_values, we need to handle
                # it here!
                assert value not in self.valid_values, value
                url = QUrl(value)
            return urlutils.proxy_from_url(url)
        except (urlutils.InvalidUrlError, urlutils.InvalidProxyTypeError) as e:
            raise configexc.ValidationError(value, e)

    def complete(self):
        out = []
        for val in self.valid_values:
            out.append((val, self.valid_values.descriptions[val]))
        out.append(('http://', 'HTTP proxy URL'))
        out.append(('socks://', 'SOCKS proxy URL'))
        out.append(('socks://localhost:9050/', 'Tor via SOCKS'))
        out.append(('http://localhost:8080/', 'Local HTTP proxy'))
        out.append(('pac+https://example.com/proxy.pac', 'Proxy autoconfiguration file URL'))
        return out


class SearchEngineUrl(BaseType):

    """A search engine URL."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        if not ('{}' in value or '{0}' in value):
            raise configexc.ValidationError(value, "must contain \"{}\"")

        try:
            value.format("")
        except (KeyError, IndexError) as e:
            raise configexc.ValidationError(
                value, "may not contain {...} (use {{ and }} for literal {/})")
        except ValueError as e:
            raise configexc.ValidationError(value, str(e))

        url = QUrl(value.replace('{}', 'foobar'))
        if not url.isValid():
            raise configexc.ValidationError(
                value, "invalid url, {}".format(url.errorString()))

        return value


class FuzzyUrl(BaseType):

    """A single URL."""

    def to_py(self, value):
        from qutebrowser.utils import urlutils
        self._basic_py_validation(value, str)
        if not value:
            return None

        try:
            return urlutils.fuzzy_url(value, do_search=False)
        except urlutils.InvalidUrlError as e:
            raise configexc.ValidationError(value, str(e))


PaddingValues = collections.namedtuple('PaddingValues', ['top', 'bottom',
                                                         'left', 'right'])


class Padding(Dict):

    """Setting for paddings around elements."""

    _show_valtype = False

    def __init__(self, none_ok=False, valid_values=None):
        super().__init__(keytype=String(), valtype=Int(minval=0),
                         fixed_keys=['top', 'bottom', 'left', 'right'],
                         none_ok=none_ok)
        # FIXME:conf
        assert valid_values is None, valid_values

    def to_py(self, value):
        d = super().to_py(value)
        if not d:
            return None
        return PaddingValues(**d)


class Encoding(BaseType):

    """Setting for a python encoding."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None
        try:
            codecs.lookup(value)
        except LookupError:
            raise configexc.ValidationError(value, "is not a valid encoding!")
        return value


class Position(MappingType):

    """The position of the tab bar."""

    MAPPING = {
        'top': QTabWidget.North,
        'bottom': QTabWidget.South,
        'left': QTabWidget.West,
        'right': QTabWidget.East,
    }

    def __init__(self, none_ok=False):
        super().__init__(
            none_ok,
            valid_values=ValidValues('top', 'bottom', 'left', 'right'))


class TextAlignment(MappingType):

    """Alignment of text."""

    MAPPING = {
        'left': Qt.AlignLeft,
        'right': Qt.AlignRight,
        'center': Qt.AlignCenter,
    }

    def __init__(self, none_ok=False):
        super().__init__(
            none_ok,
            valid_values=ValidValues('left', 'right', 'center'))


class VerticalPosition(String):

    """The position of the download bar."""

    def __init__(self, none_ok=False):
        super().__init__(none_ok=none_ok)
        self.valid_values = ValidValues('top', 'bottom')


class Url(BaseType):

    """A URL."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        qurl = QUrl.fromUserInput(value)
        if not qurl.isValid():
            raise configexc.ValidationError(value, "invalid URL - "
                                            "{}".format(qurl.errorString()))
        return qurl


class SessionName(BaseType):

    """The name of a session."""

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None
        if value.startswith('_'):
            raise configexc.ValidationError(value, "may not start with '_'!")
        return value


class SelectOnRemove(MappingType):

    """Which tab to select when the focused tab is removed."""

    MAPPING = {
        'prev': QTabBar.SelectLeftTab,
        'next': QTabBar.SelectRightTab,
        'last-used': QTabBar.SelectPreviousTab,
    }

    def __init__(self, none_ok=False):
        super().__init__(
            none_ok,
            valid_values=ValidValues(
                ('prev', "Select the tab which came before the closed one "
                 "(left in horizontal, above in vertical)."),
                ('next', "Select the tab which came after the closed one "
                 "(right in horizontal, below in vertical)."),
                ('last-used', "Select the previously selected tab.")))


class ConfirmQuit(FlagList):

    """Whether to display a confirmation when the window is closed."""

    # Values that can be combined with commas
    combinable_values = ('multiple-tabs', 'downloads')

    def __init__(self, none_ok=False):
        super().__init__(none_ok)
        self.valtype.none_ok = none_ok
        self.valtype.valid_values = ValidValues(
            ('always', "Always show a confirmation."),
            ('multiple-tabs', "Show a confirmation if "
             "multiple tabs are opened."),
            ('downloads', "Show a confirmation if "
             "downloads are running"),
            ('never', "Never show a confirmation."))

    def to_py(self, value):
        values = super().to_py(value)
        if not values:
            return None

        # Never can't be set with other options
        if 'never' in values and len(values) > 1:
            raise configexc.ValidationError(
                values, "List cannot contain never!")
        # Always can't be set with other options
        elif 'always' in values and len(values) > 1:
            raise configexc.ValidationError(
                values, "List cannot contain always!")

        return values


class NewTabPosition(String):

    """How new tabs are positioned."""

    def __init__(self, none_ok=False):
        super().__init__(none_ok=none_ok)
        self.valid_values = ValidValues(
            ('prev', "Before the current tab."),
            ('next', "After the current tab."),
            ('first', "At the beginning."),
            ('last', "At the end."))


class TimestampTemplate(BaseType):

    """An strftime-like template for timestamps.

    See
    https://docs.python.org/3/library/datetime.html#strftime-strptime-behavior
    for reference.
    """

    def to_py(self, value):
        self._basic_py_validation(value, str)
        if not value:
            return None

        try:
            # Dummy check to see if the template is valid
            datetime.datetime.now().strftime(value)
        except ValueError as error:
            # thrown on invalid template string
            raise configexc.ValidationError(
                value, "Invalid format string: {}".format(error))

        return value
