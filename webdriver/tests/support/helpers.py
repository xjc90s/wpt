import base64
import collections
import math
import sys
import os
from urllib.parse import urlparse

import webdriver

from tests.support import defaults
from tests.support.sync import Poll


def ignore_exceptions(f):
    def inner(session, *args, **kwargs):
        # Do not try to clean up already ended session.
        if session.session_id is None:
            return
        try:
            return f(session, *args, **kwargs)
        except webdriver.error.WebDriverException as e:
            print("Ignored exception %s" % e, file=sys.stderr)
    inner.__name__ = f.__name__
    return inner


def cleanup_session(session):
    """Clean-up the current session for a clean state."""
    @ignore_exceptions
    def _dismiss_user_prompts(session):
        """Dismiss any open user prompts in windows."""
        current_window = session.window_handle

        for window in _windows(session):
            session.window_handle = window
            try:
                session.alert.dismiss()
            except webdriver.NoSuchAlertException:
                pass

        session.window_handle = current_window

    @ignore_exceptions
    def _ensure_valid_window(session):
        """If current window was closed, ensure to have a valid one selected."""
        try:
            session.window_handle
        except webdriver.NoSuchWindowException:
            handles = session.handles
            if handles:
                # Update only when there is at least one valid window left.
                session.window_handle = handles[0]

    @ignore_exceptions
    def _restore_timeouts(session):
        """Restore modified timeouts to their default values."""
        session.timeouts.implicit = defaults.IMPLICIT_WAIT_TIMEOUT
        session.timeouts.page_load = defaults.PAGE_LOAD_TIMEOUT
        session.timeouts.script = defaults.SCRIPT_TIMEOUT

    @ignore_exceptions
    def _restore_window_state(session):
        """Reset window to an acceptable size.

        This also includes bringing it out of maximized, minimized,
        or fullscreen state.
        """
        if session.capabilities.get("setWindowRect"):
            session.window.size = defaults.WINDOW_SIZE

    @ignore_exceptions
    def _restore_windows(session):
        """Close superfluous windows opened by the test.

        It will not end the session implicitly by closing the last window.
        """
        current_window = session.window_handle

        for window in _windows(session, exclude=[current_window]):
            session.window_handle = window
            if len(session.handles) > 1:
                session.window.close()

        session.window_handle = current_window

    _restore_timeouts(session)
    _ensure_valid_window(session)
    _dismiss_user_prompts(session)
    _restore_windows(session)
    _restore_window_state(session)
    _switch_to_top_level_browsing_context(session)


@ignore_exceptions
def _switch_to_top_level_browsing_context(session):
    """If the current browsing context selected by WebDriver is a
    `<frame>` or an `<iframe>`, switch it back to the top-level
    browsing context.
    """
    session.switch_frame(None)


def _windows(session, exclude=None):
    """Set of window handles, filtered by an `exclude` list if
    provided.
    """
    if exclude is None:
        exclude = []
    wins = [w for w in session.handles if w not in exclude]
    return set(wins)


def clear_all_cookies(session):
    """Removes all cookies associated with the current active document"""
    session.transport.send("DELETE", "session/%s/cookie" % session.session_id)


def deep_update(source, overrides):
    """
    Update a nested dictionary or similar mapping.
    Modify ``source`` in place.
    """
    for key, value in overrides.items():
        if isinstance(value, collections.abc.Mapping) and value:
            source[key] = deep_update(source.get(key, {}), value)
        elif isinstance(value, list) and isinstance(source.get(key), list) and value:
            # Concatenate lists, ensuring all elements are kept without duplicates
            source[key] = list(dict.fromkeys(source[key] + value))
        else:
            source[key] = value

    return source


def document_dimensions(session):
    return tuple(session.execute_script("""
        const {devicePixelRatio} = window;
        const {width, height} = document.documentElement.getBoundingClientRect();
        return [width * devicePixelRatio, height * devicePixelRatio];
        """))


def center_point(element):
    """Calculates the in-view center point of a web element."""
    inner_width, inner_height = element.session.execute_script(
        "return [window.innerWidth, window.innerHeight]")
    rect = element.rect

    # calculate the intersection of the rect that is inside the viewport
    visible = {
        "left": max(0, min(rect["x"], rect["x"] + rect["width"])),
        "right": min(inner_width, max(rect["x"], rect["x"] + rect["width"])),
        "top": max(0, min(rect["y"], rect["y"] + rect["height"])),
        "bottom": min(inner_height, max(rect["y"], rect["y"] + rect["height"])),
    }

    # arrive at the centre point of the visible rectangle
    x = (visible["left"] + visible["right"]) / 2.0
    y = (visible["top"] + visible["bottom"]) / 2.0

    # convert to CSS pixels, as centre point can be float
    return (math.floor(x), math.floor(y))


def document_hidden(session):
    return session.execute_script("return document.hidden")


def document_location(session):
    """
    Unlike ``webdriver.Session#url``, which always returns
    the top-level browsing context's URL, this returns
    the current browsing context's active document's URL.
    """
    return session.execute_script("return document.location.href")


def element_rect(session, element):
    return session.execute_script("""
        let element = arguments[0];
        let rect = element.getBoundingClientRect();

        return {
            x: rect.left + window.pageXOffset,
            y: rect.top + window.pageYOffset,
            width: rect.width,
            height: rect.height,
        };
        """, args=(element,))


def is_element_in_viewport(session, element):
    """Check if element is outside of the viewport"""
    return session.execute_script("""
        let el = arguments[0];

        let rect = el.getBoundingClientRect();
        let viewport = {
          height: window.innerHeight || document.documentElement.clientHeight,
          width: window.innerWidth || document.documentElement.clientWidth,
        };

        return !(rect.right < 0 || rect.bottom < 0 ||
            rect.left > viewport.width || rect.top > viewport.height)
    """, args=(element,))


def is_fullscreen(session):
    # At the time of writing, WebKit does not conform to the
    # Fullscreen API specification.
    #
    # Remove the prefixed fallback when
    # https://bugs.webkit.org/show_bug.cgi?id=158125 is fixed.
    return session.execute_script("""
        return !!(window.fullScreen || document.webkitIsFullScreen)
        """)


def _get_maximized_state(session):
    dimensions = session.execute_script("""
        return {
            availWidth: screen.availWidth,
            availHeight: screen.availHeight,
            windowWidth: window.outerWidth,
            windowHeight: window.outerHeight,
        }
        """)

    # The maximized window can still have a border attached which would
    # cause its dimensions to exceed the whole available screen.
    return (dimensions["windowWidth"] >= dimensions["availWidth"] and
        dimensions["windowHeight"] >= dimensions["availHeight"] and
        # Only return true if the window is not in fullscreen mode
        not is_fullscreen(session)
    )


def is_maximized(session, original_rect):
    if _get_maximized_state(session):
        return True

    # Wayland doesn't guarantee that the window will get maximized
    # to the screen, so check if the dimensions got larger.
    elif is_wayland():
        dimensions = session.execute_script("""
            return {
                windowWidth: window.outerWidth,
                windowHeight: window.outerHeight,
            }
            """)
        return (
            dimensions["windowWidth"] > original_rect["width"] and
            dimensions["windowHeight"] > original_rect["height"] and
            # Only return true if the window is not in fullscreen mode
            not is_fullscreen(session)
        )
    else:
        return False


def is_not_maximized(session):
    return not _get_maximized_state(session)


def is_wayland():
    # We don't use mozinfo.display here to make sure it also
    # works upstream in wpt Github repo.
    return os.environ.get("WAYLAND_DISPLAY", "") != ""


def filter_dict(source, d):
    """Filter `source` dict to only contain same keys as `d` dict.

    :param source: dictionary to filter.
    :param d: dictionary whose keys determine the filtering.
    """
    return {k: source[k] for k in d.keys()}


def filter_supported_key_events(all_events, expected):
    events = [filter_dict(e, expected[0]) for e in all_events]
    if len(events) > 0 and events[0]["code"] is None:
        # Remove 'code' entry if browser doesn't support it
        expected = [filter_dict(e, {"key": "", "type": ""}) for e in expected]
        events = [filter_dict(e, expected[0]) for e in events]

    return (events, expected)


def get_origin_from_url(url):
    parsed_uri = urlparse(url)
    return '{uri.scheme}://{uri.netloc}'.format(uri=parsed_uri)


def wait_for_new_handle(session, handles_before):
    def find_new_handle(session):
        new_handles = list(set(session.handles) - set(handles_before))
        assert len(new_handles) == 1, "No new window was opened"
        return new_handles[0]

    wait = Poll(session, timeout=5)
    return wait.until(find_new_handle)


def get_extension_path(filename):
    return os.path.join(
        os.path.abspath(os.path.dirname(__file__)), "webextensions", filename
    )


def get_base64_for_extension_file(filename):
    with open(
        get_extension_path(filename),
        "rb",
    ) as file:
        return base64.b64encode(file.read()).decode("utf-8")
