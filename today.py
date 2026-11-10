import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
# Issues and pull requests permissions not needed at the moment, but may be used in the future
HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME'] # 'Andrew6rant'
# Forks of huge upstream projects: walking their history times out (HTTP 502) and none of it is my code
SKIP_REPOS = {'zkm/linux'}
# Count my commits here, but not lines: GitHub can't size some of these commits, so deletions would swamp additions
NO_LOC_REPOS = {'zkm/dotfiles'}
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazerCount
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    for err in request.json().get('errors') or []:
        # GitHub nulls out a repository it can't resolve (e.g. one the token can't read) and reports why here
        print('graph_repos_stars() GraphQL error:', err.get('type'), err.get('message'), err.get('path'))
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


LOC_QUERY = '''
query ($repo_name: String!, $owner: String!, $cursor: String, $page_size: Int!, $author_id: ID!) {
    repository(name: $repo_name, owner: $owner) {
        defaultBranchRef {
            target {
                ... on Commit {
                    history(first: $page_size, after: $cursor, author: {id: $author_id}) {
                        edges {
                            node {
                                ... on Commit {
                                    committedDate
                                }
                                deletions
                                additions
                            }
                        }
                        pageInfo {
                            endCursor
                            hasNextPage
                        }
                    }
                }
            }
        }
    }
}'''
# GitHub returns 502/504 (or a 200 with a timeout in `errors`) when a query takes too long to compute,
# such as additions/deletions for a page of large commits, or commit counts for forks of huge projects.
# Paginated queries fall back to progressively smaller pages.
PAGE_SIZES = (100, 50, 25, 10, 5)  # commits per page in recursive_loc
REPO_PAGE_SIZES = (60, 30, 10, 5)  # repositories per page in loc_query
REQUEST_TIMEOUT = 60     # seconds; a hung connection otherwise stalls the workflow indefinitely
RATE_LIMIT_RETRIES = 3   # per page, on 403/429 secondary rate limits


def history_present(body):
    """
    True if the response has a usable commit history, or shows the repository is empty (no default branch).
    False when the query as a whole failed, e.g. a 200 with `data: null` and a timeout in `errors`.
    """
    repository = (body.get('data') or {}).get('repository')
    if repository is None:
        return False
    branch = repository.get('defaultBranchRef')
    return branch is None or ((branch.get('target') or {}).get('history') is not None)


def repositories_present(body):
    """
    True if the response has a usable page of repositories.
    """
    return ((body.get('data') or {}).get('user') or {}).get('repositories') is not None


def post_with_page_fallback(funct_id, target, query, variables, page_sizes, size_index, has_data):
    """
    Posts one page of a paginated GraphQL query, moving to the next smaller size in page_sizes when GitHub times out.
    `variables` gets `page_size` filled in on each attempt; has_data(body) decides whether a 200 response is usable.
    Returns (response_json, size_index) on success, where size_index is the page size that worked,
    so the next page can start there instead of re-failing the larger sizes.
    Returns (failed_request, None) if every attempt failed; failed_request may be None after a network error.
    """
    request = None
    rate_limit_waits = 0
    while size_index < len(page_sizes):
        page_size = page_sizes[size_index]
        query_count(funct_id)
        try:
            # Not simple_request(): callers need to save the cache file before raising
            request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': {**variables, 'page_size': page_size}},
                                    headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            request = None
            reason = type(e).__name__
        else:
            if request.status_code == 200:
                try:
                    body = request.json()
                except ValueError:
                    body = {}
                errors = body.get('errors') or []
                if has_data(body):
                    return body, size_index
                reason = 'GraphQL error: ' + '; '.join(err.get('message', '?') for err in errors) if errors else 'malformed response'
            elif request.status_code in (403, 429) and rate_limit_waits < RATE_LIMIT_RETRIES and (
                    'Retry-After' in request.headers or 'rate limit' in request.text.lower()):
                rate_limit_waits += 1
                retry_after = request.headers.get('Retry-After', '')
                wait = int(retry_after) if retry_after.isdigit() else 60
                print(f'{funct_id}() hit a rate limit for {target}, waiting {wait}s')
                time.sleep(wait)
                continue  # same page size: the request wasn't too big, just too soon
            elif request.status_code >= 500:
                reason = f'HTTP {request.status_code}'
            else:
                return request, None  # other 4xx won't get better with a smaller page

        size_index += 1
        if size_index < len(page_sizes):
            print(f'{funct_id}() got {reason} for {target} with page size {page_size}, retrying with {page_sizes[size_index]}')
            time.sleep(2 ** size_index)  # 2, 4, 8, 16s
        else:
            print(f'{funct_id}() got {reason} for {target} with page size {page_size}, giving up')
    return request, None


def raise_request_failure(funct_id, target, request):
    """
    Raises the Exception for a request that post_with_page_fallback() gave up on.
    """
    if request is None:
        raise Exception(f'{funct_id}() could not reach GitHub for', target, QUERY_COUNT)
    if request.status_code in (403, 429):
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception(f'{funct_id}() has failed with a', request.status_code, request.text, QUERY_COUNT)


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to count the additions, deletions and commits I authored in a repository.
    History is filtered by author server-side, which keeps huge forks (e.g. zkm/drupal, ~48k commits) from being paged in full.
    Pages are walked in a loop rather than by recursion, so small page sizes can't hit Python's recursion limit.
    Returns 0 if the repository is empty.
    """
    size_index = 0
    while True:
        variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor, 'author_id': OWNER_ID['id']}
        result, size_index = post_with_page_fallback('recursive_loc', f'{owner}/{repo_name}', LOC_QUERY, variables, PAGE_SIZES, size_index, history_present)
        if size_index is None:
            force_close_file(data, cache_comment) # saves what is currently in the file before this program crashes
            raise_request_failure('recursive_loc', f'{owner}/{repo_name}', result)
        if result.get('errors'):
            # Partial data is fine: GitHub reports commits it can't size as SERVICE_UNAVAILABLE
            # field errors and nulls them out, and loc_counter_one_repo skips those.
            print(f'recursive_loc() skipping {len(result["errors"])} unsizable commit(s) in {owner}/{repo_name}')

        branch = result['data']['repository']['defaultBranchRef']
        if branch is None: # Only count commits if repo isn't empty
            return 0
        history = branch['target']['history']
        addition_total, deletion_total, my_commits = loc_counter_one_repo(owner, repo_name, history, addition_total, deletion_total, my_commits)
        if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
            return addition_total, deletion_total, my_commits
        cursor = history['pageInfo']['endCursor']


def loc_counter_one_repo(owner, repo_name, history, addition_total, deletion_total, my_commits):
    """
    Adds up the LOC values for one page of commits. The history is already filtered to commits authored by me.
    """
    for node in history['edges']:
        commit = node and node['node']
        if commit:
            my_commits += 1
            if f'{owner}/{repo_name}' not in NO_LOC_REPOS:
                addition_total += commit['additions'] or 0  # None when GitHub can't size the commit
                deletion_total += commit['deletions'] or 0
    return addition_total, deletion_total, my_commits


def loc_query(owner_affiliation, comment_size=0, force_cache=False):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Starts at 60 repos per page and falls back to smaller pages when GitHub times out: the commit counts for
    forks of huge projects (e.g. zkm/linux, ~1.5M commits) can push a page past GitHub's ~10s limit.
    Returns the total number of lines of code in all repositories
    """
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String, $page_size: Int!) {
        user(login: $login) {
            repositories(first: $page_size, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    edges, cursor, size_index = [], None, 0
    while True:
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        result, size_index = post_with_page_fallback('loc_query', f'{USER_NAME} repositories', query, variables, REPO_PAGE_SIZES, size_index, repositories_present)
        if size_index is None:
            raise_request_failure('loc_query', f'{USER_NAME} repositories', result)
        repositories = result['data']['user']['repositories']
        edges += repositories['edges']
        if not repositories['pageInfo']['hasNextPage']:
            break
        cursor = repositories['pageInfo']['endCursor']
    edges = [e for e in edges if e['node']['nameWithOwner'] not in SKIP_REPOS]
    return cache_builder(edges, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError: # If the repo is empty
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] # only save the comment
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def add_archive():
    """
    Several repositories I have contributed to have since been deleted.
    This function adds them using their last known data
    """
    with open('cache/repository_archive.txt', 'r') as f:
        data = f.readlines()
    old_data = data
    data = data[7:len(data)-3] # remove the comment block    
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if (my_commits.isdigit()): added_commits += int(my_commits)
    added_commits += int(old_data[-1].split()[4][:-1])
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]

def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data:
        if node and node['node']: # null when GitHub couldn't resolve the repository
            total_stars += node['node']['stargazerCount']
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data, 49)
    justify_format(root, 'commit_data', commit_data, 23)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 13)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    Andrew Grant (Andrew6rant), 2022-2025
    """
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    # e.g {'id': 'MDQ6VXNlcjU3MzMxMTM0'} and 2019-11-03T21:15:07Z for username 'Andrew6rant'
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(1982, 12, 22))
    formatter('age calculation', age_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    # several repositories that I've contributed to have since been deleted.
    if OWNER_ID == {'id': 'MDQ6VXNlcjU3MzMxMTM0'}: # only calculate for user Andrew6rant
        archived_data = add_archive()
        for index in range(len(total_loc)-1):
            total_loc[index] += archived_data[index]
        contrib_data += archived_data[-1]
        commit_data += int(archived_data[-2])

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) # format added, deleted, and total LOC

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    # move cursor to override 'Calculation times:' with 'Total function time:' and the total function time, then move cursor back
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
        '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time)),
        ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
