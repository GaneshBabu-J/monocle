# MIT License
# Copyright (c) 2019 Fabien Boucher

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import logging

import statistics
from copy import deepcopy
from datetime import datetime
from itertools import groupby
from monocle.utils import dbdate_to_datetime
from monocle.utils import float_trunc

from elasticsearch.helpers import scan as scanner

log = logging.getLogger(__name__)

public_queries = (
    "count_events",
    "count_authors",
    "events_histo",
    "repos_top_merged",
    "repos_top_opened",
    "events_top_authors",
    "changes_top_approval",
    "changes_top_commented",
    "changes_top_reviewed",
    "authors_top_reviewed",
    "authors_top_commented",
    "authors_top_merged",
    "authors_top_opened",
    "peers_exchange_strength",
    "change_merged_count_by_duration",
    "change_merged_avg_duration",
    "changes_closed_ratios",
    "first_comment_on_changes",
    "first_review_on_changes",
    "cold_changes",
    "hot_changes",
    "changes_lifecycle_histos",
    "changes_lifecycle_stats",
    "changes_review_histos",
    "changes_review_stats",
    "most_active_authors_stats",
    "most_reviewed_authors_stats",
    "last_merged_changes",
    "last_review_events",
    "last_comment_events",
    "last_opened_changes",
    "last_state_changed_changes",
    "oldest_open_changes",
    "changes_and_events",
    "last_abandoned_changes",
    "new_contributors",
)


def generate_events_filter(params, qfilter):
    gte = params.get('gte')
    lte = params.get('lte')
    on_cc_gte = params.get('on_cc_gte')
    on_cc_lte = params.get('on_cc_lte')
    approval = params.get('approval')
    ec_same_date = params.get('ec_same_date')

    on_created_at_range = {"on_created_at": {"format": "epoch_millis"}}
    if ec_same_date:
        on_cc_gte = gte
        on_cc_lte = lte
    if on_cc_gte or ec_same_date:
        on_created_at_range['on_created_at']['gte'] = on_cc_gte
    if on_cc_lte or ec_same_date:
        on_created_at_range['on_created_at']['lte'] = on_cc_lte
    qfilter.append({"range": on_created_at_range})
    if approval:
        qfilter.append({'term': {"approval": approval}})


def generate_changes_filter(params, qfilter):
    state = params.get('state')
    if state:
        qfilter.append({"term": {"state": state}})


def generate_filter(repositories, params):
    gte = params.get('gte')
    lte = params.get('lte')
    etype = params.get('etype')
    authors = params.get('authors')
    on_authors = params.get('on_authors')
    exclude_authors = params.get('exclude_authors')
    created_at_range = {"created_at": {"format": "epoch_millis"}}
    change_ids = params.get('change_ids')
    if gte:
        created_at_range['created_at']['gte'] = gte
    if lte:
        created_at_range['created_at']['lte'] = lte
    repositories = [
        {"regexp": {"repository_fullname": {"value": repo_regexp}}}
        for repo_regexp in repositories
    ]
    qfilter = [
        {"bool": {"should": repositories}},
        {"range": created_at_range},
    ]
    qfilter.append({"terms": {"type": etype}})
    if authors:
        qfilter.append({"terms": {"author": authors}})
    if on_authors:
        qfilter.append({"terms": {"on_author": on_authors}})
    if change_ids:
        qfilter.append({"terms": {"change_id": change_ids}})
    if 'Change' in params['etype']:
        generate_changes_filter(params, qfilter)
    else:
        generate_events_filter(params, qfilter)

    must_not = []
    if exclude_authors:
        must_not.append({"terms": {"author": exclude_authors}})
        must_not.append({"terms": {"on_author": exclude_authors}})
    ret = {"bool": {"filter": qfilter, "must_not": must_not}}
    log.debug("query EL filter: %s" % ret)
    return ret


def switch_to_on_authors(params):
    if params.get('authors'):
        # We want the events happening on changes authored by the selected authors
        params['on_authors'] = params.get('authors')
        del params['authors']
        if 'exclude_authors' in params:
            # We don't want to exclude any events authors in that context
            del params['exclude_authors']


def run_query(es, index, body):
    search_params = {'index': index, 'doc_type': index, 'body': body}
    try:
        log.debug('run_query "%s"' % search_params)
        res = es.search(**search_params)
    except Exception:
        log.exception('Unable to run query: "%s"' % search_params)
        return []
    return res


def _scan(es, index, repositories, params):
    body = {
        # "_source": "change_id",
        "_source": params.get('field', []),
        "query": generate_filter(repositories, params),
    }
    scanner_params = {'index': index, 'doc_type': index, 'query': body}
    data = scanner(es, **scanner_params)
    return [d['_source'] for d in data]


def _first_created_event(es, index, repositories, params):
    body = {
        "sort": [{"created_at": {"order": "asc"}}],
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    data = [r['_source'] for r in data['hits']['hits']]
    if data:
        return data[0]['created_at']


def count_events(es, index, repositories, params):
    body = {"query": generate_filter(repositories, params)}
    count_params = {'index': index, 'doc_type': index}
    count_params['body'] = body
    try:
        res = es.count(**count_params)
    except Exception:
        return {}
    return res['count']


def count_authors(es, index, repositories, params):
    body = {
        "aggs": {
            "agg1": {"cardinality": {"field": "author", "precision_threshold": 3000}}
        },
        "size": 0,
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    return data['aggregations']['agg1']['value']


def events_histo(es, index, repositories, params):
    def interval_to_format(fmt):
        if fmt.endswith('h'):
            return 'yyyy-MM-dd HH:00'
        if fmt.endswith('d') or fmt.endswith('w'):
            return 'yyyy-MM-dd'
        if fmt.endswith('M'):
            return 'yyyy-MM'
        if fmt.endswith('y'):
            return 'yyyy'
        return 'yyyy-MM-dd HH:mm'

    body = {
        "aggs": {
            "agg1": {
                "date_histogram": {
                    "field": "created_at",
                    "interval": params['interval'],
                    "format": interval_to_format(params['interval']),
                    "min_doc_count": 0,
                    "extended_bounds": {"min": params['gte'], "max": params['lte']},
                }
            },
            "avg_count": {"avg_bucket": {"buckets_path": "agg1>_count"}},
        },
        "size": 0,
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    return (
        data['aggregations']['agg1']['buckets'],
        data['aggregations']['avg_count']['value'] or 0,
    )


def _events_top(es, index, repositories, field, params):
    body = {
        "aggs": {
            "agg1": {
                "terms": {"field": field, "size": 1000, "order": {"_count": "desc"}}
            }
        },
        "size": 0,
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    count_series = [b['doc_count'] for b in data['aggregations']['agg1']['buckets']]
    count_avg = statistics.mean(count_series) if count_series else 0
    count_median = statistics.median(sorted(count_series)) if count_series else 0
    _from = params['from']
    _to = params['from'] + params['size']
    buckets = data['aggregations']['agg1']['buckets']
    return {
        'items': buckets[_from:_to],
        'count_avg': count_avg,
        'count_median': count_median,
        'total': len(buckets),
        'total_hits': data['hits']['total'],
    }


def repos_top_merged(es, index, repositories, params):
    params = deepcopy(params)
    switch_to_on_authors(params)
    params['etype'] = ("ChangeMergedEvent",)
    return _events_top(es, index, repositories, "repository_fullname", params)


def repos_top_opened(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("Change",)
    params['state'] = 'OPEN'
    return _events_top(es, index, repositories, "repository_fullname", params)


def events_top_authors(es, index, repositories, params):
    return _events_top(es, index, repositories, "author", params)


# TODO(fbo): add tests for queries below
def changes_top_approval(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeReviewedEvent",)
    return _events_top(es, index, repositories, "approval", params)


def changes_top_commented(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeCommentedEvent",)
    return _events_top(es, index, repositories, "change_id", params)


def changes_top_reviewed(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeReviewedEvent",)
    return _events_top(es, index, repositories, "change_id", params)


def authors_top_reviewed(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeReviewedEvent",)
    return _events_top(es, index, repositories, "on_author", params)


def authors_top_commented(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeCommentedEvent",)
    return _events_top(es, index, repositories, "on_author", params)


def authors_top_merged(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeMergedEvent",)
    switch_to_on_authors(params)
    return _events_top(es, index, repositories, "on_author", params)


def authors_top_opened(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("Change",)
    params['state'] = 'OPEN'
    return _events_top(es, index, repositories, "author", params)


def peers_exchange_strength(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeReviewedEvent", "ChangeCommentedEvent")
    # Fetch the most active authors for those events
    authors = [
        bucket['key']
        for bucket in _events_top(es, index, repositories, "author", params)['items']
    ]
    peers_strength = {}
    # For each of them get authors they most review or comment
    for author in authors:
        params['authors'] = [author]
        ret = _events_top(es, index, repositories, "on_author", params)['items']
        for bucket in ret:
            if bucket['key'] == author:
                continue
            # Build a peer identifier
            peers_id = tuple(sorted((author, bucket['key'])))
            peers_strength.setdefault(peers_id, 0)
            # Cumulate the score
            peers_strength[peers_id] += bucket['doc_count']
    peers_strength = sorted(
        [(peers_id, strength) for peers_id, strength in peers_strength.items()],
        key=lambda x: x[1],
        reverse=True,
    )
    return peers_strength


def change_merged_count_by_duration(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("Change",)
    params['state'] = "MERGED"
    body = {
        "aggs": {
            "agg1": {
                "range": {
                    "field": "duration",
                    "ranges": [
                        {"to": 24 * 3600},
                        {"from": 24 * 3600 + 1, "to": 7 * 24 * 3600},
                        {"from": 7 * 24 * 3600 + 1, "to": 31 * 24 * 3600},
                        {"from": 31 * 24 * 3600 + 1},
                    ],
                    "keyed": True,
                }
            }
        },
        "size": 0,
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    return data['aggregations']['agg1']['buckets']


def change_merged_avg_duration(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("Change",)
    params['state'] = "MERGED"
    body = {
        "aggs": {"agg1": {"avg": {"field": "duration"}}},
        "size": 0,
        "docvalue_fields": [{"field": "created_at", "format": "date_time"}],
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    return data['aggregations']['agg1']


def changes_closed_ratios(es, index, repositories, params):
    params = deepcopy(params)
    switch_to_on_authors(params)
    etypes = (
        'ChangeCreatedEvent',
        "ChangeMergedEvent",
        "ChangeAbandonedEvent",
        "ChangeCommitPushedEvent",
        "ChangeCommitForcePushedEvent",
    )
    ret = {}
    for etype in etypes:
        params['etype'] = (etype,)
        ret[etype] = count_events(es, index, repositories, params)
    try:
        ret['merged/created'] = round(
            ret['ChangeMergedEvent'] / ret['ChangeCreatedEvent'] * 100, 1
        )
    except ZeroDivisionError:
        ret['merged/created'] = 0
    try:
        ret['abandoned/created'] = round(
            ret['ChangeAbandonedEvent'] / ret['ChangeCreatedEvent'] * 100, 1
        )
    except ZeroDivisionError:
        ret['abandoned/created'] = 0
    try:
        ret['iterations/created'] = round(
            (ret['ChangeCommitPushedEvent'] + ret['ChangeCommitForcePushedEvent'])
            / ret['ChangeCreatedEvent']
            + 1,
            1,
        )
    except ZeroDivisionError:
        ret['iterations/created'] = 1
    for etype in etypes:
        del ret[etype]
    return ret


def _first_event_on_changes(es, index, repositories, params):
    params = deepcopy(params)

    def keyfunc(x):
        return x['change_id']

    groups = {}
    _events = _scan(es, index, repositories, params)
    _events = sorted(_events, key=lambda k: k['change_id'])
    # Keep by Change the created date + first event date
    for pr, events in groupby(_events, keyfunc):
        groups[pr] = {
            'change_created_at': None,
            'first_event_created_at': datetime.now(),
            'first_event_author': None,
            'delta': None,
        }
        for event in events:
            if not groups[pr]['change_created_at']:
                groups[pr]['change_created_at'] = dbdate_to_datetime(
                    event['on_created_at']
                )
            event_created_at = dbdate_to_datetime(event['created_at'])
            if event_created_at < groups[pr]['first_event_created_at']:
                groups[pr]['first_event_created_at'] = event_created_at
                groups[pr]['delta'] = (
                    groups[pr]['first_event_created_at']
                    - groups[pr]['change_created_at']
                )
                groups[pr]['first_event_author'] = event['author']
    ret = {'first_event_delay_avg': 0, 'top_authors': {}}
    for pr_data in groups.values():
        ret['first_event_delay_avg'] += pr_data['delta'].seconds
        ret['top_authors'].setdefault(pr_data['first_event_author'], 0)
        ret['top_authors'][pr_data['first_event_author']] += 1
    try:
        ret['first_event_delay_avg'] = int(ret['first_event_delay_avg'] / len(groups))
    except ZeroDivisionError:
        ret['first_event_delay_avg'] = 0
    ret['top_authors'] = sorted(
        [(k, v) for k, v in ret['top_authors'].items()],
        key=lambda x: x[1],
        reverse=True,
    )[:10]
    return ret


def first_comment_on_changes(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ('ChangeCommentedEvent',)
    return _first_event_on_changes(es, index, repositories, params)


def first_review_on_changes(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ('ChangeReviewedEvent',)
    return _first_event_on_changes(es, index, repositories, params)


def cold_changes(es, index, repositories, params):
    params = deepcopy(params)
    size = params.get('size')
    params['etype'] = ('Change',)
    params['state'] = 'OPEN'
    changes = _scan(es, index, repositories, params)
    _changes_ids = set([change['change_id'] for change in changes])
    params['etype'] = ('ChangeCommentedEvent', 'ChangeReviewedEvent')
    del params['state']
    events = _scan(es, index, repositories, params)
    _events_ids = set([event['change_id'] for event in events])
    changes_ids_wo_rc = _changes_ids.difference(_events_ids)
    changes_wo_rc = [
        change for change in changes if change['change_id'] in changes_ids_wo_rc
    ]
    items = sorted(changes_wo_rc, key=lambda x: dbdate_to_datetime(x['created_at']))
    if size:
        items = items[:size]
    return {'items': items}


def hot_changes(es, index, repositories, params):
    params = deepcopy(params)
    size = params.get('size')
    # Set a significant depth to get an 'accurate' median value
    params['size'] = 500
    top_commented_changes = changes_top_commented(es, index, repositories, params)
    # Keep changes with comment events > median
    top_commented_changes = [
        change
        for change in top_commented_changes['items']
        if change['doc_count'] > top_commented_changes['count_median']
    ]
    mapping = {}
    for top_commented_change in top_commented_changes:
        mapping[top_commented_change['key']] = top_commented_change['doc_count']
    change_ids = [_id['key'] for _id in top_commented_changes]
    if not change_ids:
        return []
    _params = {
        'etype': ('Change',),
        'state': 'OPEN',
        'change_ids': change_ids,
    }
    changes = _scan(es, index, repositories, _params)
    for change in changes:
        change['hot_score'] = mapping[change['change_id']]
    items = sorted(changes, key=lambda x: x['hot_score'], reverse=True)
    if size:
        items = items[:size]
    return {'items': items}


def changes_lifecycle_histos(es, index, repositories, params):
    params = deepcopy(params)
    switch_to_on_authors(params)
    ret = {}
    etypes = (
        'ChangeCreatedEvent',
        "ChangeMergedEvent",
        "ChangeAbandonedEvent",
        "ChangeCommitPushedEvent",
        "ChangeCommitForcePushedEvent",
    )
    for etype in etypes:
        params['etype'] = (etype,)
        ret[etype] = events_histo(es, index, repositories, params)
    return ret


def changes_lifecycle_stats(es, index, repositories, params):
    params = deepcopy(params)
    switch_to_on_authors(params)
    ret = {}
    ret['ratios'] = changes_closed_ratios(es, index, repositories, params)
    ret['histos'] = changes_lifecycle_histos(es, index, repositories, params)
    etypes = (
        'ChangeCreatedEvent',
        "ChangeMergedEvent",
        "ChangeAbandonedEvent",
        "ChangeCommitPushedEvent",
        "ChangeCommitForcePushedEvent",
    )
    ret['avgs'] = {}
    for etype in etypes:
        ret['avgs'][etype] = float_trunc(ret['histos'][etype][-1])
        params['etype'] = (etype,)
        events_count = count_events(es, index, repositories, params)
        authors_count = count_authors(es, index, repositories, params)
        ret[etype] = {'events_count': events_count, 'authors_count': authors_count}
    return ret


def changes_review_histos(es, index, repositories, params):
    params = deepcopy(params)
    ret = {}
    etypes = ('ChangeCommentedEvent', "ChangeReviewedEvent")
    for etype in etypes:
        params['etype'] = (etype,)
        ret[etype] = events_histo(es, index, repositories, params)
    return ret


def changes_review_stats(es, index, repositories, params):
    params = deepcopy(params)
    ret = {}
    ret['first_event_delay'] = {}
    ret['first_event_delay']['comment'] = first_comment_on_changes(
        es, index, repositories, params
    )
    ret['first_event_delay']['review'] = first_review_on_changes(
        es, index, repositories, params
    )
    ret['histos'] = changes_review_histos(es, index, repositories, params)
    for etype in ("ChangeReviewedEvent", "ChangeCommentedEvent"):
        params['etype'] = (etype,)
        events_count = count_events(es, index, repositories, params)
        authors_count = count_authors(es, index, repositories, params)
        ret[etype] = {'events_count': events_count, 'authors_count': authors_count}
    return ret


def most_active_authors_stats(es, index, repositories, params):
    params = deepcopy(params)
    ret = {}
    for etype in ("ChangeCreatedEvent", "ChangeReviewedEvent", "ChangeCommentedEvent"):
        params['etype'] = (etype,)
        ret[etype] = events_top_authors(es, index, repositories, params)
    switch_to_on_authors(params)
    ret["ChangeMergedEvent"] = authors_top_merged(es, index, repositories, params)
    return ret


def most_reviewed_authors_stats(es, index, repositories, params):
    return {
        "reviewed": authors_top_reviewed(es, index, repositories, params),
        "commented": authors_top_commented(es, index, repositories, params),
    }


def last_merged_changes(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("Change",)
    params['state'] = "MERGED"
    body = {
        "sort": [{"closed_at": {"order": "desc"}}],
        "size": params['size'],
        "from": params['from'],
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    changes = [r['_source'] for r in data['hits']['hits']]
    return {'items': changes, 'total': data['hits']['total']}


def last_opened_changes(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("Change",)
    params['state'] = "OPEN"
    body = {
        "sort": [{"created_at": {"order": "desc"}}],
        "size": params['size'],
        "from": params['from'],
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    changes = [r['_source'] for r in data['hits']['hits']]
    return {'items': changes, 'total': data['hits']['total']}


def last_events(es, index, repositories, params):
    params = deepcopy(params)
    body = {
        "sort": [{"created_at": {"order": "desc"}}],
        "size": params['size'],
        "from": params['from'],
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    events = [r['_source'] for r in data['hits']['hits']]
    return {'items': events, 'total': data['hits']['total']}


def last_review_events(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeReviewedEvent",)
    return last_events(es, index, repositories, params)


def last_comment_events(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("ChangeCommentedEvent",)
    return last_events(es, index, repositories, params)


def last_state_changed_changes(es, index, repositories, params):
    return {
        "merged_changes": last_merged_changes(es, index, repositories, params),
        "opened_changes": last_opened_changes(es, index, repositories, params),
    }


def oldest_open_changes(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("Change",)
    params['state'] = "OPEN"
    body = {
        "sort": [{"created_at": {"order": "asc"}}],
        "size": params['size'],
        "from": params['from'],
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    changes = [r['_source'] for r in data['hits']['hits']]
    return {'items': changes, 'total': data['hits']['total']}


def changes_and_events(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = (
        "Change",
        'ChangeCreatedEvent',
        "ChangeMergedEvent",
        "ChangeAbandonedEvent",
        "ChangeCommitPushedEvent",
        "ChangeCommitForcePushedEvent",
        "ChangeReviewedEvent",
        "ChangeCommentedEvent",
    )
    body = {
        "sort": [{"created_at": {"order": "asc"}}],
        "size": params['size'],
        "from": params['from'],
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    changes = [r['_source'] for r in data['hits']['hits']]
    return {'items': changes, 'total': data['hits']['total']}


def last_abandoned_changes(es, index, repositories, params):
    params = deepcopy(params)
    params['etype'] = ("Change",)
    params['state'] = "CLOSED"
    body = {
        "sort": [{"created_at": {"order": "desc"}}],
        "size": params['size'],
        "from": params['from'],
        "query": generate_filter(repositories, params),
    }
    data = run_query(es, index, body)
    changes = [r['_source'] for r in data['hits']['hits']]
    return {'items': changes, 'total': data['hits']['total']}


def new_contributors(es, index, repositories, params):
    params = deepcopy(params)
    params['size'] = 10000
    new_authors = events_top_authors(es, index, repositories, params)['items']
    new = set([x['key'] for x in new_authors])
    params['lte'] = params['gte']
    del params['gte']
    old = set(
        [x['key'] for x in events_top_authors(es, index, repositories, params)['items']]
    )
    diff = new.difference(old)
    return {'items': [n for n in new_authors if n['key'] in diff]}
