import re
import sublime
import sublime_plugin
import threading
import subprocess
import os


def get_node_path():
    node_path_yargs = ['which', 'node']
    node_path = subprocess.check_output(node_path_yargs)

    return node_path.strip()


def open_require(view, requirePath):
    node_path = get_node_path()

    if not node_path:
        return

    js = (
        "try {"
        "  var resolved = require.resolve('" + requirePath + "');"
        "  process.stdout.write(resolved);"
        "} catch () {}"
    )
    yargs = [node_path, '-e', js]
    working_dir = os.path.dirname(os.path.realpath(view.file_name()))

    file_name = subprocess.check_output(yargs, cwd=working_dir)
    file_name = file_name.decode('utf-8').strip()

    if file_name and os.path.isfile(file_name):
        view.window().open_file(file_name)


class OpenRequire(sublime_plugin.TextCommand):
    def is_visible(self):
        return False

    def run(self, edit):
        syntax = self.view.settings().get('syntax')

        if 'JavaScript' not in syntax:
            return

        if self.view.id() in UrlHighlighter.urls_for_view:
            selection = self.view.sel()[0]
            if selection.empty():
                selection = next(
                    (
                        url for url
                        in UrlHighlighter.urls_for_view[self.view.id()]
                        if url.contains(selection)
                    ), None)
                if not selection:
                    return
            requirePath = self.view.substr(selection)
            open_require(self.view, requirePath)


class UrlHighlighter(sublime_plugin.EventListener):
    URL_REGEX = "require\\('[^']*'\\)"
    DEFAULT_MAX_URLS = 200
    SETTINGS_FILENAME = 'require-links.sublime-settings'

    urls_for_view = {}
    scopes_for_view = {}
    ignored_views = []
    highlight_semaphore = threading.Semaphore()

    # def onPostSave(self, view):
    #     print("just got saved")

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

        return 'JavaScript' in syntax

    """The logic entry point. Find all URLs in view, store & highlight them"""
    def update_url_highlights(self, view):

        if not self.should_highlight(view):
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

    def calculate_region(self, view, region):
        pattern = "(require\\(')([^']*)'\\)"
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
                u'require-links ' + scope_name,
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
                u'require-links ' + scope_name,
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
                view.erase_regions(u'clickable-urls ' + unused_scope_name)

        UrlHighlighter.scopes_for_view[view.id()] = new_scopes
