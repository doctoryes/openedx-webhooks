from __future__ import print_function, unicode_literals

import os
import functools
import requests
import bugsnag
from urlobject import URLObject

STUDIO_CROWD_TOKENKEY = None


def pop_dict_id(d):
    id = d["id"]
    del d["id"]
    return (id, d)


def paginated_get(url, session=None, limit=None, per_page=100, **kwargs):
    """
    Retrieve all objects from a paginated API.

    Assumes that the pagination is specified in the "link" header, like
    Github's v3 API.

    The `limit` describes how many results you'd like returned.  You might get
    more than this, but you won't make more requests to the server once this
    limit has been exceeded.  For example, paginating by 100, if you set a
    limit of 250, three requests will be made, and you'll get 300 objects.

    """
    url = URLObject(url).set_query_param('per_page', str(per_page))
    limit = limit or 999999999
    session = session or requests.Session()
    returned = 0
    while url:
        resp = session.get(url, **kwargs)
        result = resp.json()
        if not resp.ok:
            bugsnag.configure_request(meta_data={
                "session_headers": session.headers,
                "session_cookies": session.cookies,
            })
            raise requests.exceptions.RequestException(result["message"])
        for item in result:
            yield item
            returned += 1
        url = None
        if resp.links and returned < limit:
            url = resp.links.get("next", {}).get("url", "")


def jira_paginated_get(url, session=None,
                       start=0, start_param="startAt", obj_name=None,
                       retries=3, **fields):
    """
    Like ``paginated_get``, but uses JIRA's conventions for a paginated API, which
    are different from Github's conventions.
    """
    session = session or requests.Session()
    global STUDIO_CROWD_TOKENKEY
    if STUDIO_CROWD_TOKENKEY:
        session.cookies["studio.crowd.tokenkey"] = STUDIO_CROWD_TOKENKEY
    url = URLObject(url)
    more_results = True
    while more_results:
        result_url = (
            url.set_query_param(start_param, str(start))
               .set_query_params(**fields)
        )
        for _ in xrange(retries):
            try:
                result_resp = session.get(result_url)
                result = result_resp.json()
                break
            except ValueError:
                continue
        if not result_resp.ok:
            bugsnag.configure_request(meta_data={
                "session_headers": session.headers,
                "session_cookies": session.cookies,
            })
            raise requests.exceptions.RequestException(result_resp.text)
        result = result_resp.json()
        if not result:
            break
        if obj_name:
            objs = result[obj_name]
        else:
            objs = result
        for obj in objs:
            yield obj
        returned = len(objs)
        total = result["total"]
        if start + returned < total:
            start += returned
        else:
            more_results = False


def jira_group_members(groupname, session=None, start=0, retries=3):
    """
    JIRA's group members API is horrible. This makes it easier to use.
    """
    session = session or requests.Session()
    url = URLObject("/rest/api/2/group").set_query_param("groupname", groupname)
    more_results = True
    while more_results:
        end = start + 49  # max 50 users per page
        expand = "users[{start}:{end}]".format(start=start, end=end)
        result_url = url.set_query_param("expand", expand)
        for _ in xrange(retries):
            try:
                result_resp = session.get(result_url)
                result = result_resp.json()
                break
            except ValueError:
                continue
        if not result_resp.ok:
            bugsnag.configure_request(meta_data={
                "session_headers": session.headers,
                "session_cookies": session.cookies,
            })
            raise requests.exceptions.RequestException(result_resp.text)
        result = result_resp.json()
        if not result:
            break
        users = result["users"]["items"]
        for user in users:
            yield user
        returned = start + len(users)
        total = result["users"]["size"]
        if start + returned < total:
            start += returned
        else:
            more_results = False


def jira_users(session=None):
    """
    JIRA has an API for returning all users, but it's not ready for primetime.
    It's used only by the admin pages, and it does authentication based on
    session cookies only. We'll use it anyway, since there is no alternative.
    """
    base_url = getattr(session, "base_url", None)
    # make a new session
    session = requests.Session()
    global STUDIO_CROWD_TOKENKEY
    if not STUDIO_CROWD_TOKENKEY:
        JIRA_USERNAME = os.environ.get("JIRA_USERNAME")
        JIRA_PASSWORD = os.environ.get("JIRA_PASSWORD")
        if not JIRA_USERNAME or not JIRA_PASSWORD:
            raise ValueError("Must set JIRA_USERNAME and JIRA_PASSWORD to list users.")
            login_url = "https://openedx.atlassian.net/login"
            payload = {"username": JIRA_USERNAME, "password": JIRA_PASSWORD}
            login_resp = session.post(login_url, data=payload, allow_redirects=False)
            if not login_resp.status_code in (200, 303):
                raise requests.exceptions.RequestException(login_resp.text)
            STUDIO_CROWD_TOKENKEY = login_resp.cookies["studio.crowd.tokenkey"]
    if not "studio.crowd.tokenkey" in session.cookies:
        session.cookies["studio.crowd.tokenkey"] = STUDIO_CROWD_TOKENKEY

    if base_url:
        url = URLObject(base_url).relative("/admin/rest/um/1/user/search")
    else:
        url = "/admin/rest/um/1/user/search"

    for user in jira_paginated_get(url, start_param="start-index", session=session):
        yield user


def memoize(func):
    cache = {}

    def mk_key(*args, **kwargs):
        return (tuple(args), tuple(sorted(kwargs.items())))

    @functools.wraps(func)
    def memoized(*args, **kwargs):
        key = memoized.mk_key(*args, **kwargs)
        try:
            return cache[key]
        except KeyError:
            cache[key] = func(*args, **kwargs)
            return cache[key]

    memoized.mk_key = mk_key

    def uncache(*args, **kwargs):
        key = memoized.mk_key(*args, **kwargs)
        if key in cache:
            del cache[key]
            return True
        else:
            return False

    memoized.uncache = uncache

    return memoized


def memoize_except(values):
    """
    Just like normal `memoize`, but don't cache when the function returns
    certain values. For example, you could use this to make a function not
    cache `None`.
    """
    if not isinstance(values, (list, tuple)):
        values = (values,)

    def decorator(func):
        cache = {}

        def mk_key(*args, **kwargs):
            return (tuple(args), tuple(sorted(kwargs.items())))

        @functools.wraps(func)
        def memoized(*args, **kwargs):
            key = memoized.mk_key(*args, **kwargs)
            try:
                return cache[key]
            except KeyError:
                value = func(*args, **kwargs)
                if value not in values:
                    cache[key] = value
                return value

        memoized.mk_key = mk_key

        def uncache(*args, **kwargs):
            key = memoized.mk_key(*args, **kwargs)
            if key in cache:
                del cache[key]
                return True
            else:
                return False

        memoized.uncache = uncache

        return memoized

    return decorator


def to_unicode(s):
    if isinstance(s, unicode):
        return s
    return s.decode('utf-8')
