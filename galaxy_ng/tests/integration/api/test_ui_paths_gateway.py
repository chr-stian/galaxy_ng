#!/usr/bin/env python3

import random
import json
import subprocess

import pytest

from orionutils.generator import build_collection
from ansible.galaxy.api import GalaxyError
from jsonschema import validate as validate_json

from galaxykit.utils import GalaxyClientError
from ..constants import DEFAULT_DISTROS, USERNAME_PUBLISHER
from ..schemas import (
    schema_collection_import,
    schema_collection_import_detail,
    schema_collectionversion,
    schema_collectionversion_metadata,
    schema_distro,
    schema_distro_repository,
    schema_ee_registry,
    schema_featureflags,
    schema_group,
    schema_me,
    schema_namespace_detail,
    schema_objectlist,
    schema_remote,
    schema_settings,
    schema_task,
    schema_ui_collection_summary,
    schema_user,
)
from ..utils import (
    UIClient,
    generate_unused_namespace,
    get_client,
    wait_for_task_ui_client,
    wait_for_task,
)
from ..utils.iqe_utils import get_paginated, remove_from_cache
from ..utils.legacy import (
    clean_all_roles,
    wait_for_v1_task
)

from .rbac_actions.utils import ReusableLocalContainer


REGEX_403 = r"HTTP Code: 403"


# /api/automation-hub/_ui/v1/auth/login/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_login(galaxy_client):
    gc = galaxy_client("basic_user")

    # an authenticated session has a csrftoken and a sessionid
    assert gc.cookies['csrftoken'] is not None
    assert gc.cookies['gateway_sessionid'] is not None


# /api/automation-hub/_ui/v1/auth/logout/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_logout(galaxy_client):
    gc = galaxy_client("basic_user")

    # check the auth first
    assert gc.cookies['csrftoken'] is not None
    assert gc.cookies['gateway_sessionid'] is not None

    gc.gw_client.logout()

    # logout should clear the sessionid but not the csrftoken
    assert gc.gw_client.cookies['csrftoken'] is not None
    assert 'sessionid' not in gc.gw_client.cookies
    remove_from_cache("basic_user")



# /api/automation-hub/_ui/v1/collection-versions/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_collection_versions(galaxy_client, uncertifiedv2):
    gc = galaxy_client('basic_user')
    ds = gc.get('_ui/v1/collection-versions/')
    validate_json(instance=ds, schema=schema_objectlist)
    assert len(ds['data']) >= 1
    for cv in ds['data']:
        validate_json(instance=cv, schema=schema_collectionversion)
        validate_json(instance=cv['metadata'], schema=schema_collectionversion_metadata)

        # try to get the direct url for this version ...
        cv_url = f"_ui/v1/collection-versions/{cv['namespace']}/{cv['name']}/{cv['version']}/"
        cv_resp = gc.get(cv_url)
        validate_json(instance=cv_resp, schema=schema_collectionversion)
        validate_json(instance=cv_resp['metadata'], schema=schema_collectionversion_metadata)

@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_collection_versions_version_range(galaxy_client, uncertifiedv2):
    """Test the ?version_range query parameter."""
    c1, c2 = uncertifiedv2
    gc = galaxy_client('basic_user')
    v_path = f"_ui/v1/collection-versions/?name={c1.name}&namespace={c1.namespace}"

    # test single version
    ds = gc.get(f'{v_path}&version_range=={c1.version}')
    assert len(ds['data']) == 1
    assert ds['data'][0]["version"] == c1.version

    # test range
    ds = gc.get(f'{v_path}&version_range>={c1.version}')
    assert len(ds['data']) == 2
    assert set([v["version"] for v in ds['data']]) == set([c1.version, c2.version])

    # test range exclusive
    ds = gc.get(f'{v_path}&version_range=>{c1.version}')
    assert len(ds['data']) == 1
    assert ds['data'][0]["version"] == c2.version

    # test invalid
    with pytest.raises(GalaxyClientError) as ctx:
        gc.get(f'{v_path}&version_range=not_a_semver_version')
    assert ctx.value.response.status_code == 400

# /api/automation-hub/_ui/v1/collection-versions/{version}/
# ^ tested by previous function


# /api/automation-hub/_ui/v1/collection_signing/
# /api/automation-hub/_ui/v1/collection_signing/{path}/
# /api/automation-hub/_ui/v1/collection_signing/{path}/{namespace}/
# /api/automation-hub/_ui/v1/collection_signing/{path}/{namespace}/{collection}/
# /api/automation-hub/_ui/v1/collection_signing/{path}/{namespace}/{collection}/{version}/
# /api/automation-hub/_ui/v1/controllers/

# /api/automation-hub/_ui/v1/distributions/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
@pytest.mark.min_hub_version("4.6dev")
def test_gw_api_ui_v1_distributions(galaxy_client):
    gc = galaxy_client('basic_user')
    ds = gc.get('_ui/v1/distributions/?limit=1000')
    validate_json(instance=ds, schema=schema_objectlist)

    for distro in ds['data']:
        validate_json(instance=distro, schema=schema_distro)
        if distro['repository']:
            validate_json(instance=distro['repository'], schema=schema_distro_repository)

    distros_to_remove = []
    for distro in ds['data']:
        if distro["name"].startswith("repo-test-") or distro["name"].startswith("dist-test-"):
            distros_to_remove.append(distro)
    for distro in distros_to_remove:
        ds['data'].remove(distro)

    # make sure all default distros are in the list ...
    distro_tuples = [(x['name'], x['base_path']) for x in ds['data']]
    for k, v in DEFAULT_DISTROS.items():
        key = (k, v['basepath'])
        # this next assert might fail if the test suite has been run before against
        # the same hub instance
        # https://issues.redhat.com/browse/AAH-2601
        try:
            assert key in distro_tuples
        except AssertionError:
            pytest.xfail("rh-certified distribution has not been found because "
                         "the distribution endpoint returns the first 100 distributions"
                         " and rh-certified is further down in the list. "
                         "This has happened because the whole test suite has been run"
                         " multiple times against the same hub instance, "
                         "leaving a lot of test data. "
                         "This is the jira to fix the test: AAH-2601")


# /api/automation-hub/_ui/v1/distributions/{pulp_id}/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
@pytest.mark.min_hub_version("4.6dev")
def test_gw_api_ui_v1_distributions_by_id(galaxy_client):
    gc = galaxy_client('basic_user')
    # get the response
    ds = gc.get('_ui/v1/distributions/')
    validate_json(instance=ds, schema=schema_objectlist)

    for distro in ds['data']:
        validate_json(instance=distro, schema=schema_distro)

    # check the endpoint for each distro by pulp id ...
    distro_ids = [x['pulp_id'] for x in ds['data']]
    for distro_id in distro_ids:
        _ds = gc.get(f'_ui/v1/distributions/{distro_id}/')
        validate_json(instance=_ds, schema=schema_distro)
        if _ds['repository']:
            validate_json(instance=_ds['repository'], schema=schema_distro_repository)
        assert _ds['pulp_id'] == distro_id


# /api/automation-hub/_ui/v1/execution-environments/registries/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_execution_environments_registries(galaxy_client):
    gc = galaxy_client('ee_admin')

    # get the response
    ds = gc.get('_ui/v1/execution-environments/registries/')
    validate_json(instance=ds, schema=schema_objectlist)

    # try to create one
    suffix = random.choice(range(0, 1000))
    rname = f'redhat.io.{suffix}'
    payload = {
        'name': rname,
        'url': 'https://registry.redhat.io',
    }
    rds = gc.post('_ui/v1/execution-environments/registries/', body=payload)
    validate_json(instance=rds, schema=schema_ee_registry)
    try:
        id = rds["id"]
    except KeyError:
        id = rds["pk"]

    # try to get it by pulp_id
    rds = gc.get(f"_ui/v1/execution-environments/registries/{id}/")
    validate_json(instance=rds, schema=schema_ee_registry)
    try:
        id = rds["id"]
    except KeyError:
        id = rds["pk"]
    # sync it
    task = gc.post(
        f"_ui/v1/execution-environments/registries/{id}/sync/",
        body={}
    )
    validate_json(instance=task, schema=schema_task)

    # wait for sync to finish
    wait_for_task_ui_client(gc, task)

    # index it
    task = gc.post(
        f"_ui/v1/execution-environments/registries/{id}/index/",
        body={}
    )
    validate_json(instance=task, schema=schema_task)

    # wait for index to finish
    wait_for_task_ui_client(gc, task)

    # delete the registry
    gc.delete(f"_ui/v1/execution-environments/registries/{id}/", parse_json=False)

    # make sure it's gone
    with pytest.raises(GalaxyClientError) as ctx:
        gc.get(f"_ui/v1/execution-environments/registries/{id}/")
    assert ctx.value.response.status_code == 404



# /api/automation-hub/_ui/v1/execution-environments/registries/{pulp_id}/
# ^ tested by previous function


# /api/automation-hub/_ui/v1/execution-environments/registries/{id}/index/
# ^ tested by previous function


# /api/automation-hub/_ui/v1/execution-environments/registries/{id}/sync/
# ^ tested by previous function


# /api/automation-hub/_ui/v1/execution-environments/remotes/
# /api/automation-hub/_ui/v1/execution-environments/remotes/{pulp_id}/

@pytest.fixture
def local_container():
    return ReusableLocalContainer('int_tests')


# /api/automation-hub/_ui/v1/feature-flags/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
@pytest.mark.min_hub_version("4.6dev")
def test_gw_api_ui_v1_feature_flags(galaxy_client):

    gc = galaxy_client('basic_user')
    # get the response
    ds = gc.get('_ui/v1/feature-flags/')
    validate_json(instance=ds, schema=schema_featureflags)

    # assert ds['ai_deny_index'] is False
    assert ds['execution_environments'] is True
    assert ds['legacy_roles'] is False


# /api/automation-hub/_ui/v1/groups/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_groups(galaxy_client):

    gc = galaxy_client('partner_engineer')
    # get the response
    ds = gc.get('_ui/v1/groups/')
    validate_json(instance=ds, schema=schema_objectlist)

    for grp in ds['data']:
        validate_json(instance=grp, schema=schema_group)

    # try to make a group
    suffix = random.choice(range(0, 1000))
    payload = {'name': f'foobar{suffix}'}
    ds = gc.post('_ui/v1/groups/', body=payload)
    validate_json(instance=ds, schema=schema_group)
    assert ds['name'] == payload['name']
    assert ds['pulp_href'].endswith(f"/{ds['id']}/")


# /api/automation-hub/_ui/v1/groups/{group_pk}/model-permissions/
# /api/automation-hub/_ui/v1/groups/{group_pk}/model-permissions/{id}/


# /api/automation-hub/_ui/v1/groups/{group_pk}/users/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_groups_users(galaxy_client):

    gc = galaxy_client('basic_user')
    groups_ds = gc.get('_ui/v1/groups/?limit=1000')
    validate_json(instance=groups_ds, schema=schema_objectlist)

    # get the primary key for PE
    pe_id = None
    for x in groups_ds['data']:
        if x['name'] == 'system:partner-engineers':
            pe_id = x['id']
            break
    assert pe_id is not None

    # validate username="jdoe" is in the group's userlist
    users_ds = gc.get(f'_ui/v1/groups/{pe_id}/users/')
    validate_json(instance=users_ds, schema=schema_objectlist)
    assert "jdoe" in [x["username"] for x in users_ds["data"]]


# /api/automation-hub/_ui/v1/groups/{group_pk}/users/{id}/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_groups_users_add_delete(galaxy_client):

    gc = galaxy_client('partner_engineer')
    suffix = random.choice(range(0, 1000))
    group_name = f'group{suffix}'
    user_name = f'user{suffix}'

    # make the group
    group_ds = gc.post('_ui/v1/groups/', body={'name': group_name})
    validate_json(instance=group_ds, schema=schema_group)
    group_id = group_ds['id']

    # make the user
    user_ds = gc.post(
        '_ui/v1/users/',
        body={
            'username': user_name,
            'first_name': 'foo',
            'last_name': 'bar',
            'email': 'foo@barz.com',
            'groups': [group_ds],
            'password': 'abcdefghijklmnopqrstuvwxyz1234567890!@#$%^&*()-+',
            'is_superuser': False
        }
    )
    validate_json(instance=user_ds, schema=schema_user)

    # validate the new user is in the group's userlist
    users_ds = gc.get(f'_ui/v1/groups/{group_id}/users/')
    validate_json(instance=users_ds, schema=schema_objectlist)
    assert user_name in [x['username'] for x in users_ds['data']]

    # remove the user from the group
    user_id = user_ds['id']
    gc.delete(f'_ui/v1/groups/{group_id}/users/{user_id}/', parse_json=False)

    # validate the new user is NOT in the group's userlist
    users_ds = gc.get(f'_ui/v1/groups/{group_id}/users/')
    validate_json(instance=users_ds, schema=schema_objectlist)
    assert user_name not in [x['username'] for x in users_ds['data']]


# /api/automation-hub/_ui/v1/groups/{id}/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_groups_by_id(galaxy_client):

    gc = galaxy_client('basic_user')
    # get the response
    ds = gc.get('_ui/v1/groups/')
    validate_json(instance=ds, schema=schema_objectlist)

    for grp in ds['data']:
        gid = grp['id']
        ds = gc.get(f'_ui/v1/groups/{gid}/')
        validate_json(instance=ds, schema=schema_group)
        assert ds['id'] == gid


# /api/automation-hub/_ui/v1/imports/collections/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_imports_collections(galaxy_client):
    gc = galaxy_client('basic_user')
    # get the response
    ds = gc.get('_ui/v1/imports/collections/')
    validate_json(instance=ds, schema=schema_objectlist)

    for job in ds['data']:
        validate_json(instance=job, schema=schema_collection_import)
        task_id = job['id']
        jurl = f'_ui/v1/imports/collections/{task_id}/'
        jds = gc.get(jurl)
        validate_json(instance=jds, schema=schema_collection_import_detail)


# /api/automation-hub/_ui/v1/imports/collections/{task_id}/
# ^ tested by the previous function


# /api/automation-hub/_ui/v1/landing-page/
# ^ tested in tests/integration/api/test_landing_page.py


# /api/automation-hub/_ui/v1/me/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_me(galaxy_client, settings):
    gc = galaxy_client('basic_user')
    # get the response
    ds = gc.get('_ui/v1/me/')
    validate_json(instance=ds, schema=schema_me)

    assert not ds['is_anonymous']
    assert ds['username'] == ds.get('username')

    if settings.get("KEYCLOAK_URL") is not None:
        assert ds['auth_provider'] == ['keycloak']
    else:
        assert ds['auth_provider'] == ['django']


# /api/automation-hub/_ui/v1/my-distributions/
# /api/automation-hub/_ui/v1/my-distributions/{pulp_id}/


# /api/automation-hub/_ui/v1/my-namespaces/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
@pytest.mark.min_hub_version("4.6dev")
def test_gw_api_ui_v1_my_namespaces(galaxy_client):
    gc = galaxy_client("partner_engineer")
    new_namespace = generate_unused_namespace(gc, api_version='_ui/v1')

    # get user
    ds = gc.get('_ui/v1/me/')

    # create ns with group
    # TODO: Add user's roles to the me endpoint
    payload = {
        'name': new_namespace,
        'groups': [{
            'id': ds['groups'][0]['id'],
            'name': ds['groups'][0]['name'],
            'object_roles': ["galaxy.collection_admin"],
        }]
    }
    gc.post('_ui/v1/my-namespaces/', body=payload)

    # get the my-namespaces view
    ds = gc.get('_ui/v1/my-namespaces/')
    validate_json(instance=ds, schema=schema_objectlist)

    # get all the namespaces in the view
    namespace_names = get_paginated(gc, '_ui/v1/my-namespaces/')
    namespace_names = [x['name'] for x in namespace_names]

    # validate the new one shows up
    for expected_ns_name in ['autohubtest2', 'autohubtest3', 'signing', new_namespace]:
        assert expected_ns_name in namespace_names

    # delete
    gc.delete(f'_ui/v1/my-namespaces/{new_namespace}/', parse_json=False)

    # get the response
    gc.get('_ui/v1/my-namespaces/')

    # confirm deletion
    namespace_names = get_paginated(gc, '_ui/v1/my-namespaces/')
    namespace_names = [x['name'] for x in namespace_names]
    assert new_namespace not in namespace_names


# /api/automation-hub/_ui/v1/my-namespaces/{name}/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
@pytest.mark.min_hub_version("4.6dev")
def test_gw_api_ui_v1_my_namespaces_name(galaxy_client):
    gc = galaxy_client('partner_engineer')
    # get the response
    resp = gc.get('_ui/v1/my-namespaces/autohubtest2/')
    validate_json(instance=resp, schema=schema_namespace_detail)


# /api/automation-hub/_ui/v1/my-synclists/
# /api/automation-hub/_ui/v1/my-synclists/{id}/
# /api/automation-hub/_ui/v1/my-synclists/{id}/curate/


# /api/automation-hub/_ui/v1/namespaces/
# ^ tested in tests/integration/api/test_namespace_management.py


# /api/automation-hub/_ui/v1/namespaces/{name}/
# ^ tested in tests/integration/api/test_namespace_management.py


# /api/automation-hub/_ui/v1/remotes/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_remotes(galaxy_client):
    gc = galaxy_client('basic_user')
    # get the response
    ds = gc.get('_ui/v1/remotes/?limit=100')
    validate_json(instance=ds, schema=schema_objectlist)

    for remote in ds['data']:
        validate_json(instance=remote, schema=schema_remote)

    remote_names = [x['name'] for x in ds['data']]
    assert 'community' in remote_names
    assert 'rh-certified' in remote_names


# /api/automation-hub/_ui/v1/remotes/{pulp_id}/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_remotes_by_id(galaxy_client):

    gc = galaxy_client('basic_user')
    # get the response
    ds = gc.get('_ui/v1/remotes/')
    validate_json(instance=ds, schema=schema_objectlist)

    for remote in ds['data']:
        validate_json(instance=remote, schema=schema_remote)

    # FIXME - there is no suitable pulp_id for a remote?
    pulp_ids = [x['pk'] for x in ds['data']]
    for pulp_id in pulp_ids:
        gc.get(f'_ui/v1/remotes/{pulp_id}/')


# /api/automation-hub/_ui/v1/repo/{distro_base_path}/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_repo_distro_by_basepath(galaxy_client):

    gc = galaxy_client('basic_user')
    # get each repo by basepath? or is it get a distro by basepath?
    for k, v in DEFAULT_DISTROS.items():
        bp = v['basepath']
        ds = gc.get(f'_ui/v1/repo/{bp}/')
        validate_json(instance=ds, schema=schema_objectlist)


# /api/automation-hub/_ui/v1/repo/{distro_base_path}/{namespace}/{name}/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_collection_detail_view(galaxy_client, published):

    namespace = published.namespace
    name = published.name
    version = published.version

    gc = galaxy_client('basic_user')
    ds = gc.get(f'_ui/v1/repo/published/{namespace}/{name}/')
    validate_json(instance=ds, schema=schema_ui_collection_summary)

    assert ds['namespace']['name'] == namespace
    assert ds['name'] == name
    assert ds['latest_version']['version'] == version
    all_versions = [x['version'] for x in ds['all_versions']]
    assert version in all_versions


# /api/automation-hub/_ui/v1/settings/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
@pytest.mark.min_hub_version("4.6dev")
def test_gw_api_ui_v1_settings(galaxy_client):
    gc = galaxy_client('basic_user')

    # get the response
    ds = gc.get('_ui/v1/settings/')
    validate_json(instance=ds, schema=schema_settings)

    # FIXME - password length and token expiration are None?
    assert ds['GALAXY_ENABLE_UNAUTHENTICATED_COLLECTION_ACCESS'] is False
    assert ds['GALAXY_ENABLE_UNAUTHENTICATED_COLLECTION_DOWNLOAD'] is False
    assert ds['GALAXY_REQUIRE_CONTENT_APPROVAL'] is True


# /api/automation-hub/_ui/v1/synclists/
# /api/automation-hub/_ui/v1/synclists/{id}/


# /api/automation-hub/_ui/v1/tags/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_tags(galaxy_client):

    gc = galaxy_client('basic_user')

    # get the response
    ds = gc.get('_ui/v1/tags/')
    validate_json(instance=ds, schema=schema_objectlist)

    # FIXME - ui tags api does not support POST?


# /api/automation-hub/_ui/v1/tags/collections/
@pytest.mark.deployment_community
def test_gw_api_ui_v1_tags_collections(ansible_config, upload_artifact):

    config = ansible_config("basic_user")
    api_client = get_client(config)

    def build_upload_wait(tags):
        artifact = build_collection(
            "skeleton",
            config={
                "namespace": USERNAME_PUBLISHER,
                "tags": tags,
            }
        )
        resp = upload_artifact(config, api_client, artifact)
        resp = wait_for_task(api_client, resp)

    build_upload_wait(["tools", "database", "postgresql"])
    build_upload_wait(["tools", "database", "mysql"])
    build_upload_wait(["tools", "database"])
    build_upload_wait(["tools"])

    with UIClient(config=config) as uclient:

        # get the response
        resp = uclient.get('_ui/v1/tags/collections')
        assert resp.status_code == 200

        ds = resp.json()
        validate_json(instance=ds, schema=schema_objectlist)

        resp = uclient.get('_ui/v1/tags/collections?name=tools')
        ds = resp.json()
        assert len(ds["data"]) == 1

        # result count should be 2 (mysql, postgresql)
        resp = uclient.get('_ui/v1/tags/collections?name__icontains=sql')
        ds = resp.json()
        assert len(ds["data"]) == 2

        resp = uclient.get('_ui/v1/tags/collections?name=test123')
        ds = resp.json()
        assert len(ds["data"]) == 0

        # verify sort by name is correct
        resp = uclient.get('_ui/v1/tags/collections?sort=name')
        tags = [tag["name"] for tag in resp.json()["data"]]
        assert tags == sorted(tags)

        # verify sort by -count is correct
        resp = uclient.get('_ui/v1/tags/collections/?sort=-count')
        data = resp.json()["data"]
        assert data[0]["name"] == "tools"
        assert data[1]["name"] == "database"


# /api/automation-hub/_ui/v1/tags/roles/
@pytest.mark.deployment_community
def test_gw_api_ui_v1_tags_roles(ansible_config):
    """Test endpoint's sorting and filtering"""

    def _sync_role(github_user, role_name):
        pargs = json.dumps({"github_user": github_user, "role_name": role_name}).encode('utf-8')
        resp = api_admin_client('/api/v1/sync/', method='POST', args=pargs)
        assert isinstance(resp, dict)
        assert resp.get('task') is not None
        assert resp.get('pulp_id') is not None
        wait_for_v1_task(resp=resp, api_client=api_admin_client)

    def _populate_tags_cmd():
        proc = subprocess.run(
            "django-admin populate-role-tags",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
        )
        assert proc.returncode == 0

    config = ansible_config("basic_user")

    admin_config = ansible_config("admin")
    api_admin_client = get_client(
        admin_config,
        request_token=False,
        require_auth=True
    )

    with UIClient(config=config) as uclient:

        # get the response
        resp = uclient.get('_ui/v1/tags/roles')
        assert resp.status_code == 200

        ds = resp.json()
        validate_json(instance=ds, schema=schema_objectlist)

        # clean all roles ...
        clean_all_roles(ansible_config)

        # start the sync
        _sync_role("geerlingguy", "docker")

        resp = uclient.get('_ui/v1/tags/roles')
        resp.status_code == 200
        aggregate_total = sum([x['count'] for x in resp.json()['data']])
        assert aggregate_total == 0

        # run command to populate role tags table
        _populate_tags_cmd()

        resp = uclient.get('_ui/v1/tags/roles')
        resp.status_code == 200
        aggregate_total = sum([x['count'] for x in resp.json()['data']])
        assert aggregate_total > 0

        # add additional tags to test count
        # tags ["docker", "system"]
        _sync_role("6nsh", "docker")
        # tags ["docker"]
        _sync_role("0x28d", "docker_ce")
        _populate_tags_cmd()

        resp = uclient.get('_ui/v1/tags/roles?sort=-count')
        resp.status_code == 200
        assert resp.json()["meta"]["count"] > 0

        # test correct count sorting
        tags = [tag for tag in uclient.get('_ui/v1/tags/roles').json()["data"]]

        assert sorted(tags, key=lambda r: r["count"], reverse=True)[:2] == resp.json()["data"][:2]
        assert resp.json()["data"][0]["name"] == "docker"
        assert resp.json()["data"][1]["name"] == "system"


# /api/automation-hub/_ui/v1/users/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_users(galaxy_client):
    gc = galaxy_client('partner_engineer')
    # get the response
    ds = gc.get('_ui/v1/users/')
    validate_json(instance=ds, schema=schema_objectlist)

    assert len(ds['data']) >= 1
    for user in ds['data']:
        validate_json(instance=user, schema=schema_user)

    # try creating a user
    suffix = random.choice(range(0, 9999))
    payload = {
        'username': f'foobar{suffix}',
        'first_name': 'foobar',
        'last_name': f'{suffix}'
    }
    ds = gc.post('_ui/v1/users/', body=payload)
    validate_json(instance=ds, schema=schema_user)

    # should NOT be superuser by default
    assert not ds['is_superuser']

    assert ds['username'] == payload['username']
    assert ds['first_name'] == payload['first_name']
    assert ds['last_name'] == payload['last_name']


# /api/automation-hub/_ui/v1/users/{id}/
@pytest.mark.deployment_standalone
@pytest.mark.api_ui
def test_gw_api_ui_v1_users_by_id(galaxy_client):
    gc = galaxy_client('partner_engineer')
    resp = gc.get('_ui/v1/users/?username=jdoe')
    id = resp["data"][0]["id"]

    resp = gc.get('_ui/v1/groups/?name=system:partner-engineers')
    group_id = resp["data"][0]["id"]

    # get the response
    ds = gc.get(f'_ui/v1/users/{id}/')
    validate_json(instance=ds, schema=schema_user)

    assert ds['id'] == id
    assert ds['username'] == 'jdoe'
    # assert ds['is_superuser'] is False
    assert {'id': group_id, 'name': 'system:partner-engineers'} in ds['groups']


# /api/automation-hub/_ui/v1/users/
@pytest.mark.deployment_cloud
@pytest.mark.api_ui
def test_users_list_insights_access(ansible_config):
    """Check insights mode access to users endpoint"""

    config = ansible_config("basic_user")
    api_prefix = config.get("api_prefix").rstrip("/")
    url = f"{api_prefix}/_ui/v1/users/"
    api_client = get_client(config, request_token=True, require_auth=True)

    with pytest.raises(GalaxyError, match=REGEX_403):
        api_client(url, method="GET")

    with pytest.raises(GalaxyError, match=REGEX_403):
        api_client(url, method="POST", args=b"{}")

    config = ansible_config("partner_engineer")
    api_client = get_client(config, request_token=True, require_auth=True)

    resp = api_client(url, method="GET")
    assert "data" in resp.keys()

    with pytest.raises(GalaxyError, match=REGEX_403):
        api_client(url, method="POST", args=b"{}")


# /api/automation-hub/_ui/v1/users/{id}/
@pytest.mark.deployment_cloud
@pytest.mark.api_ui
def test_users_detail_insights_access(ansible_config):
    """Check insights mode access to users endpoint"""

    config = ansible_config("basic_user")
    api_prefix = config.get("api_prefix").rstrip("/")
    api_client = get_client(config, request_token=True, require_auth=True)

    admin_config = ansible_config("partner_engineer")
    admin_client = get_client(admin_config, request_token=True, require_auth=True)

    user_id = admin_client(
        f"{api_prefix}/_ui/v1/users/?username={config['username']}")["data"][0]["id"]
    url = f"{api_prefix}/_ui/v1/users/{user_id}/"

    with pytest.raises(GalaxyError, match=REGEX_403):
        api_client(url, method="GET")

    with pytest.raises(GalaxyError, match=REGEX_403):
        api_client(url, method="PUT")

    with pytest.raises(GalaxyError, match=REGEX_403):
        api_client(url, method="DELETE")

    api_client = admin_client

    user = api_client(url, method="GET")
    assert user["id"] == user_id

    print(user)

    put_resp = api_client(url, method="PUT", args=user)
    assert put_resp == user

    with pytest.raises(GalaxyError, match=REGEX_403):
        api_client(url, method="DELETE")