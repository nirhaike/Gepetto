import functools
import json
import os
import re
import textwrap
import threading
import traceback
import gettext

import idaapi
import ida_hexrays
import ida_kernwin
import idc
import openai

# =============================================================================
# EDIT VARIABLES IN THIS SECTION
# =============================================================================

# Set your API key here, or put it in the OPENAI_API_KEY environment variable.
openai.api_key = ""

# Specify the program language. It can be "fr_FR", "zh_CN", or any folder in gepetto-locales.
# Defaults to English.
language = ""

# Determines whether the program should print output in a more verbose manner.
debug = False

# =============================================================================
# END
# =============================================================================

# Set up translations
translate = gettext.translation('gepetto',
                                os.path.join(os.path.abspath(os.path.dirname(__file__)), "gepetto-locales"),
                                fallback=True,
                                languages=[language])
_ = translate.gettext

# =============================================================================
# Setup the context menu and hotkey in IDA
# =============================================================================

class GepettoPlugin(idaapi.plugin_t):
    flags = 0
    explain_action_name = "gepetto:explain_function"
    explain_menu_path = "Edit/Gepetto/" + _("Explain function")
    comments_action_name = "gepetto:auto_complete_comments"
    comments_menu_path = "Edit/Gepetto/" + _("Auto-complete comments")
    rename_action_name = "gepetto:rename_function"
    rename_menu_path = "Edit/Gepetto/" + _("Rename variables")
    wanted_name = 'Gepetto'
    wanted_hotkey = ''
    comment = _("Uses gpt-3.5-turbo to enrich the decompiler's output")
    help = _("See usage instructions on GitHub")
    menu = None

    def init(self):
        # Check whether the decompiler is available
        if not ida_hexrays.init_hexrays_plugin():
            return idaapi.PLUGIN_SKIP

        # Function explaining action
        explain_action = idaapi.action_desc_t(self.explain_action_name,
                                              _('Explain function'),
                                              ExplainHandler(),
                                              "Ctrl+Alt+G",
                                              _('Use gpt-3.5-turbo to explain the currently selected function'),
                                              199)
        idaapi.register_action(explain_action)
        idaapi.attach_action_to_menu(self.explain_menu_path, self.explain_action_name, idaapi.SETMENU_APP)

        # Explain inline comments action
        comments_action = idaapi.action_desc_t(self.comments_action_name,
                                               _('Auto-complete comments'),
                                               ExplainFurtherHandler(),
                                               "Ctrl+Alt+J",
                                               _('Use gpt-3.5-turbo to treat comments as python-like format strings and auto-complete them'),
                                               199)
        idaapi.register_action(comments_action)
        idaapi.attach_action_to_menu(self.comments_menu_path, self.comments_action_name, idaapi.SETMENU_APP)

        # Variable renaming action
        rename_action = idaapi.action_desc_t(self.rename_action_name,
                                             _('Rename variables'),
                                             RenameHandler(),
                                             "Ctrl+Alt+R",
                                             _("Use gpt-3.5-turbo to rename this function's variables"),
                                             199)
        idaapi.register_action(rename_action)
        idaapi.attach_action_to_menu(self.rename_menu_path, self.rename_action_name, idaapi.SETMENU_APP)

        # Register context menu actions
        self.menu = ContextMenuHooks()
        self.menu.hook()

        return idaapi.PLUGIN_KEEP

    def run(self, arg):
        pass

    def term(self):
        idaapi.detach_action_from_menu(self.explain_menu_path, self.explain_action_name)
        idaapi.detach_action_from_menu(self.comments_menu_path, self.comments_action_name)
        idaapi.detach_action_from_menu(self.rename_menu_path, self.rename_action_name)
        if self.menu:
            self.menu.unhook()
        return

# -----------------------------------------------------------------------------

class ContextMenuHooks(idaapi.UI_Hooks):
    def finish_populating_widget_popup(self, form, popup):
        # Add actions to the context menu of the Pseudocode view
        if idaapi.get_widget_type(form) == idaapi.BWN_PSEUDOCODE:
            idaapi.attach_action_to_popup(form, popup, GepettoPlugin.explain_action_name, "Gepetto/")
            idaapi.attach_action_to_popup(form, popup, GepettoPlugin.comments_action_name, "Gepetto/")
            idaapi.attach_action_to_popup(form, popup, GepettoPlugin.rename_action_name, "Gepetto/")

# -----------------------------------------------------------------------------

def inline_comments_callback(address, view, decompiled_func, response, retries=0):
    """
    Callback that sets the inline comments at the requested places.
    :param address: The address of the function to comment
    :param view: A handle to the decompiler window
    :param decompiled_func: The decompiler's output
    :param response: The comment to add
    """
    if debug:
        print(response)

    kwargs = extract_json_or_retry(response, retries, inline_comments_callback,
        address=address, view=view, decompiled_func=decompiled_func)
    if not kwargs:
        return

    # Numeric format string arguments are supplied as positional arguments
    make_args = lambda kwargs: [kwargs.get(str(i)) for i in range(max(map(int, filter(str.isdigit, kwargs.keys()))) + 1)]
    args = make_args(kwargs)

    for key in decompiled_func.user_cmts:
        try:
            cmt = decompiled_func.user_cmts[key].c_str()
            decompiled_func.set_user_cmt(key, cmt.format(*args, **kwargs))
        except:
            if debug:
                traceback.print_exc()

    decompiled_func.save_user_cmts()

    # Refresh the window so the comment is displayed properly
    if view:
        view.refresh_view(False)
    print("davinci-003 query finished!")

# -----------------------------------------------------------------------------

def shrink_decompilation(decompilation, window=320):
    '''
    Reduces the size of the function's decompilation so it'll fit in the "ExplainFurther" query.
    The resulting shrinked decompilation will contain all the comments containing format strings.
    :param decompilation: The decompilation generated by hex-rays
    :param window: The amount of additional context to give, before the first comment
        and after the last one.
    '''
    start_match = re.search(r'(\{[0-9A-Za-z_]*\})', decompilation)
    end_match = re.search(r'.*(\{[0-9A-Za-z_]*\})', decompilation)
    start = start_match.start() if start_match is not None else 0
    end = end_match.end() if end_match is not None else len(decompilation)

    if start > 0:
        text = decompilation
        func_content_start = decompilation.find('\n{\n') + 2
        sw = 0
        if start-window > 0:
            sw = max(func_content_start, start-window)
            title = decompilation[:func_content_start]
            content = text[sw:].split('\n', 1)[-1]
            text = '\n'.join([title, '// ...', content])
        if end+window-sw < len(text) - 1:
            text = text[:end+window-sw].rsplit('\n', 1)[0]
            text = '\n'.join([text, '// ...', '}'])
        return text

    return decompilation

# -----------------------------------------------------------------------------

class ExplainFurtherHandler(idaapi.action_handler_t):
    """
    This handler is tasked with querying gpt-3.5-turbo for an explanation of the
    given function at specific places, by auto-completing the format string specifiers
    in the comments.
    """
    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    def activate(self, ctx):
        decompiler_output = ida_hexrays.decompile(idaapi.get_screen_ea())
        v = ida_hexrays.get_widget_vdui(ctx.widget)
        query_model_async("Analyze the following C function. Treat the comments as python format string (formatted with curly braces), and " + \
                          "complete the comments. Reply with a single JSON where the keys are the values in the curly braces (without the braces), " + \
                          "and the values are the matching substituted strings.\n" + \
                          "The completions should make up valuable comments. Print only the json, without any other explanation.\n\n" \
                          + shrink_decompilation(str(decompiler_output)),
                          functools.partial(inline_comments_callback,
                            address=idaapi.get_screen_ea(),
                            view=v,
                            decompiled_func=decompiler_output))
        if debug:
            print(shrink_decompilation(str(decompiler_output)))
        return 1

    # This action is always available.
    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS

# -----------------------------------------------------------------------------

def comment_callback(address, view, response):
    """
    Callback that sets a comment at the given address.
    :param address: The address of the function to comment
    :param view: A handle to the decompiler window
    :param response: The comment to add
    """
    response = "\n".join(textwrap.wrap(response, 80, replace_whitespace=False))

    # Add the response as a comment in IDA, but preserve any existing non-Gepetto comment
    comment = idc.get_func_cmt(address, 0)
    comment = re.sub(r'----- ' + _("Comment generated by Gepetto") + ' -----.*?----------------------------------------',
                     r"",
                     comment,
                     flags=re.DOTALL)

    idc.set_func_cmt(address, '----- ' + _("Comment generated by Gepetto") +
                     f" -----\n\n"
                     f"{response.strip()}\n\n"
                     f"----------------------------------------\n\n"
                     f"{comment.strip()}", 0)
    # Refresh the window so the comment is displayed properly
    if view:
        view.refresh_view(False)
    print(_("gpt-3.5-turbo query finished!"))


# -----------------------------------------------------------------------------

class ExplainHandler(idaapi.action_handler_t):
    """
    This handler is tasked with querying gpt-3.5-turbo for an explanation of the
    given function. Once the reply is received, it is added as a function
    comment.
    """
    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    def activate(self, ctx):
        decompiler_output = ida_hexrays.decompile(idaapi.get_screen_ea())
        v = ida_hexrays.get_widget_vdui(ctx.widget)
        query_model_async(_("Can you explain what the following C function does and suggest a better name for it?\n"
                            "{decompiler_output}").format(decompiler_output=str(decompiler_output)),
                          functools.partial(comment_callback, address=idaapi.get_screen_ea(), view=v))
        return 1

    # This action is always available.
    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS

# -----------------------------------------------------------------------------

def rename_callback(address, view, response, retries=0):
    """
    Callback that extracts a JSON array of old names and new names from the
    response and sets them in the pseudocode.
    :param address: The address of the function to work on
    :param view: A handle to the decompiler window
    :param response: The response from gpt-3.5-turbo
    :param retries: The number of times that we received invalid JSON
    """
    names = extract_json_or_retry(response, retries, rename_callback, address=idaapi.get_screen_ea(), view=view)
    if not names:
        return

    # The rename function needs the start address of the function
    function_addr = idaapi.get_func(address).start_ea

    replaced = []
    for n in names:
        if ida_hexrays.rename_lvar(function_addr, n, names[n]):
            replaced.append(n)

    # Update possible names left in the function comment
    comment = idc.get_func_cmt(address, 0)
    if comment and len(replaced) > 0:
        for n in replaced:
            comment = re.sub(r'\b%s\b' % n, names[n], comment)
        idc.set_func_cmt(address, comment, 0)

    # Refresh the window to show the new names
    if view:
        view.refresh_view(True)
    print(_("gpt-3.5-turbo query finished! {replaced} variable(s) renamed.").format(replaced=len(replaced)))

# -----------------------------------------------------------------------------

def extract_json_or_retry(response, retries, retry_callback, **retry_kwargs):
    '''
    Tries to extract a valid JSON from the given response.
    Upon failure, we ask the model to fix the json and try again.
    :param response: The response from davinci-003
    :param retries: The number of times that we received invalid JSON
    :param retry_callback: The function to call after fixing the json if it is invalid.
    :retry_kwargs: The arguments to pass into the callback.
    '''
    j = re.search(r"\{[^}]*?\}", response)
    if not j:
        if retries >= 3:  # Give up obtaining the JSON after 3 times.
            print(_("Could not obtain valid data from the model, giving up. Dumping the response for manual import:"))
            print(response)
            return
        print(_("Cannot extract valid JSON from the response. Asking the model to fix it..."))
        query_model_async(_("The JSON document provided in this response is invalid. Can you fix it?\n"
                            "{response}").format(response=response),
                          functools.partial(retry_callback, **retry_kwargs, retries=retries + 1))
        return None
    try:
        data = json.loads(j.group(0))
    except json.decoder.JSONDecodeError:
        if retries >= 3:  # Give up fixing the JSON after 3 times.
            print(_("Could not obtain valid data from the model, giving up. Dumping the response for manual import:"))
            print(response)
            return
        print(_("The JSON document returned is invalid. Asking the model to fix it..."))
        query_model_async(_("Please fix the following JSON document:\n{json}").format(json=j.group(0)),
                          functools.partial(retry_callback, **retry_kwargs, retries=retries + 1))
        return None
    return data


# -----------------------------------------------------------------------------

class RenameHandler(idaapi.action_handler_t):
    """
    This handler requests new variable names from gpt-3.5-turbo and updates the
    decompiler's output.
    """
    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    def activate(self, ctx):
        decompiler_output = ida_hexrays.decompile(idaapi.get_screen_ea())
        v = ida_hexrays.get_widget_vdui(ctx.widget)
        query_model_async(_("Analyze the following C function:\n{decompiler_output}"
                            "\nSuggest better variable names, reply with a JSON array where keys are the original names "
                            "and values are the proposed names. Do not explain anything, only print the JSON "
                            "dictionary.").format(decompiler_output=str(decompiler_output)),
                          functools.partial(rename_callback, address=idaapi.get_screen_ea(), view=v))
        return 1

    # This action is always available.
    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS

# =============================================================================
# gpt-3.5-turbo interaction
# =============================================================================

def query_model(query, cb, max_tokens=2500):
    """
    Function which sends a query to gpt-3.5-turbo and calls a callback when the response is available.
    Blocks until the response is received
    :param query: The request to send to gpt-3.5-turbo
    :param cb: Tu function to which the response will be passed to.
    """
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "user", "content": query}
            ]
        )
        ida_kernwin.execute_sync(functools.partial(cb, response=response.choices[0]["message"]["content"]),
                                 ida_kernwin.MFF_WRITE)
    except openai.InvalidRequestError as e:
        # Context length exceeded. Determine the max number of tokens we can ask for and retry.
        m = re.search(r'maximum context length is (\d+) tokens, however you requested \d+ tokens \((\d+) in your '
                      r'prompt;', str(e))
        if not m:
            print(_("gpt-3.5-turbo could not complete the request: {error}").format(error=str(e)))
            return
        (hard_limit, prompt_tokens) = (int(m.group(1)), int(m.group(2)))
        max_tokens = hard_limit - prompt_tokens
        if max_tokens >= 750:
            print(_("Context length exceeded! Reducing the completion tokens to "
                    "{max_tokens}...").format(max_tokens=max_tokens))
            query_model(query, cb, max_tokens)
        else:
            print("Unfortunately, this function is too big to be analyzed with the model's current API limits.")

    except openai.OpenAIError as e:
        print(_("gpt-3.5-turbo could not complete the request: {error}").format(error=str(e)))
    except Exception as e:
        print(_("General exception encountered while running the query: {error}").format(error=str(e)))

# -----------------------------------------------------------------------------

def query_model_async(query, cb):
    """
    Function which sends a query to gpt-3.5-turbo and calls a callback when the response is available.
    :param query: The request to send to gpt-3.5-turbo
    :param cb: Tu function to which the response will be passed to.
    """
    print(_("Request to gpt-3.5-turbo sent..."))
    t = threading.Thread(target=query_model, args=[query, cb])
    t.start()

# =============================================================================
# Main
# =============================================================================

def PLUGIN_ENTRY():
    if not openai.api_key:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            print(_("Please edit this script to insert your OpenAI API key!"))
            raise ValueError("No valid OpenAI API key found")

    return GepettoPlugin()
