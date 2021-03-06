import re
import sublime
import sublime_plugin
import threading
import subprocess
import os


SCOPE_PREFIX = u'require-links '


def get_node_path():
    return sublime.load_settings(
        "require-links.sublime-settings"
    ).get('node_path', '/usr/local/bin/node')


def resolve_require_path(context_file_path, require_path):
    node_path = get_node_path()

    js = (
        "try {"
        "  var requirePath = process.argv[1];"
        "  var resolved = require.resolve(requirePath);"
        "  process.stdout.write(resolved);"
        "} catch (e) {}"
    )
    yargs = [node_path, '-e', js, require_path]
    working_dir = os.path.dirname(os.path.realpath(context_file_path))

    if not os.path.exists(working_dir) or not os.path.isdir(working_dir):
        return None

    file_name = subprocess.check_output(yargs, cwd=working_dir)
    file_name = file_name.decode('utf-8').strip()

    return file_name


def open_require(view, require_path):
    resolved_path = resolve_require_path(view.file_name(), require_path)

    if resolved_path and os.path.isfile(resolved_path):
        view.window().open_file(resolved_path)


class OpenRequire(sublime_plugin.TextCommand):
    def is_visible(self):
        return False

    def want_event(self):
        return True

    def run(self, edit, event):
        if self.view.id() not in UrlHighlighter.urls_for_view:
            return

        syntax = self.view.settings().get('syntax')

        if 'JavaScript' not in syntax:
            return

        if not self.view.file_name():
            return

        point = self.view.window_to_text((event['x'], event['y']))

        selection = next(
            (
                url for url
                in UrlHighlighter.urls_for_view[self.view.id()]
                if url.contains(point)
            ), None)

        if not selection:
            return

        require_path = self.view.substr(selection)
        open_require(self.view, require_path)


class UrlHighlighter(sublime_plugin.EventListener):
    URL_REGEX = "require\\(['\"][^'\"]*['\"]\\)"
    DEFAULT_MAX_URLS = 200
    SETTINGS_FILENAME = 'require-links.sublime-settings'

    urls_for_view = {}
    scopes_for_view = {}
    ignored_views = []
    highlight_semaphore = threading.Semaphore()

    def on_activated(self, view):
        self.update_url_highlights(view)

    # Blocking handlers for ST2
    def on_load(self, view):
        if sublime.version() < '3000':
            self.update_url_highlights(view)

    def on_modified(self, view):
        if sublime.version() < '3000':
            self.update_url_highlights(view)

    # Async listeners for ST3
    def on_load_async(self, view):
        self.update_url_highlights_async(view)

    def on_modified_async(self, view):
        self.update_url_highlights_async(view)

    def on_close(self, view):
        for map in [
            self.urls_for_view, self.scopes_for_view, self.ignored_views
        ]:
            if view.id() in map:
                del map[view.id()]

    def should_highlight(self, view):
        syntax = view.settings().get('syntax')

        if 'JavaScript' not in syntax:
            return False

        view_file = view.file_name()

        if not view_file or not os.path.isfile(view_file):
            return False

        view_file_dir = os.path.dirname(os.path.realpath(view_file))

        return os.path.exists(view_file_dir) and os.path.isdir(view_file_dir)

    """The logic entry point. Find all URLs in view, store & highlight them"""
    def update_url_highlights(self, view):

        if not self.should_highlight(view):
            self.clear_scopes(view)
            return

        max_url_limit = 200

        if view.id() in UrlHighlighter.ignored_views:
            return

        urls = view.find_all(UrlHighlighter.URL_REGEX)
        fixed_urls = []

        for u in urls:
            fixed_urls.append(self.calculate_region(view, u))

        # Avoid slowdowns for views with too much URLs
        if len(urls) > max_url_limit:
            print("UrlHighlighter: ignoring view with %u URLs" % len(urls))
            UrlHighlighter.ignored_views.append(view.id())
            return

        UrlHighlighter.urls_for_view[view.id()] = fixed_urls
        self.highlight_urls(view, fixed_urls)

    # The main regex & find_all call matches
    # regions like "require('whatever')".
    # This method narrows the region to just
    # the part in between quotes
    def calculate_region(self, view, region):
        pattern = "(require\\(['\"])([^']*)['\"]\\)"
        str_value = view.substr(region)
        match = re.match(pattern, str_value)
        left_group = match.groups()[0]
        name_group = match.groups()[1]

        new_a = region.a + len(left_group)
        new_b = new_a + len(name_group)

        return sublime.Region(new_a, new_b)

    """Same as update_url_highlights, but avoids race conditions with a
    semaphore."""
    def update_url_highlights_async(self, view):
        UrlHighlighter.highlight_semaphore.acquire()
        try:
            self.update_url_highlights(view)
        finally:
            UrlHighlighter.highlight_semaphore.release()

    """Creates a set of regions from the intersection of urls and scopes,
    underlines all of them."""
    def highlight_urls(self, view, urls):
        # We need separate regions for each lexical scope for ST
        # to use a proper color for the underline
        scope_map = {}
        for url in urls:
            scope_name = view.scope_name(url.a)
            scope_map.setdefault(scope_name, []).append(url)

        for scope_name in scope_map:
            self.underline_regions(view, scope_name, scope_map[scope_name])

        self.update_view_scopes(view, scope_map.keys())

    """Apply underlining with provided scope name to provided regions.
    Uses the empty region underline hack for Sublime Text 2 and native
    underlining for Sublime Text 3."""
    def underline_regions(self, view, scope_name, regions):
        if sublime.version() >= '3019':
            # in Sublime Text 3, the regions are just underlined
            flags = (
                sublime.DRAW_NO_FILL |
                sublime.DRAW_NO_OUTLINE |
                sublime.DRAW_SOLID_UNDERLINE
            )
            view.add_regions(
                SCOPE_PREFIX + scope_name,
                regions,
                scope_name,
                flags=flags
            )
        else:
            # in Sublime Text 2, the 'empty region underline' hack is used
            char_regions = [
                sublime.Region(pos, pos)
                for region in regions
                for pos in range(region.a, region.b)
            ]
            view.add_regions(
                SCOPE_PREFIX + scope_name,
                char_regions,
                scope_name,
                sublime.DRAW_EMPTY_AS_OVERWRITE)

    """Store new set of underlined scopes for view. Erase underlining from
    scopes that were used but are not anymore."""
    def update_view_scopes(self, view, new_scopes):
        old_scopes = UrlHighlighter.scopes_for_view.get(view.id(), None)
        if old_scopes:
            unused_scopes = set(old_scopes) - set(new_scopes)
            for unused_scope_name in unused_scopes:
                view.erase_regions(SCOPE_PREFIX + unused_scope_name)

        UrlHighlighter.scopes_for_view[view.id()] = new_scopes

    def clear_scopes(self, view):
        scopes = UrlHighlighter.scopes_for_view.get(view.id(), None)

        if not scopes:
            return

        for scope_name in scopes:
            view.erase_regions(SCOPE_PREFIX + scope_name)
