import datetime
import os
import subprocess

import pytest
import pygit2
from unittest import mock
from unittest.mock import MagicMock
from pathlib import Path

from django.conf import settings

from olympia import amo
from olympia.amo.tests import (
    addon_factory,
    version_factory,
    user_factory,
    activate_locale,
)
from olympia.git.utils import (
    AddonGitRepository,
    BrokenRefError,
    MissingMasterBranchError,
    TemporaryWorktree,
    BRANCHES,
    EXTRACTED_PREFIX,
    get_mime_type_for_blob,
    extract_version_to_git,
)

# Aliases for easier and shorter access
_blob_type = pygit2.GIT_OBJ_BLOB
_tree_type = pygit2.GIT_OBJ_TREE


def _run_process(cmd, repo):
    """Small helper to run git commands on the shell"""
    return subprocess.check_output(
        cmd,
        shell=True,
        env={'GIT_DIR': repo.git_repository.path},
        universal_newlines=True,
    )


def update_git_repo_creation_time(addon_repo, time):
    os.utime(
        # See also: `AddonGitRepository.is_recent`.
        os.path.join(
            addon_repo.git_repository_path, AddonGitRepository.GIT_DESCRIPTION
        ),
        # (access time, modification time)
        (time.timestamp(), time.timestamp()),
    )


def apply_changes(repo, version, contents, path, delete=False):
    # Apply the requested change to the git repository
    branch_name = BRANCHES[version.channel]
    git_repo = repo.git_repository
    blob_id = git_repo.create_blob(contents)

    # Initialize the index from the tree structure of the most
    # recent commit in `branch`
    tree = git_repo.revparse_single(branch_name).tree
    index = git_repo.index
    index.read_tree(tree)

    # Add / update the index
    path = os.path.join(EXTRACTED_PREFIX, path)
    if delete:
        index.remove(path)
    else:
        entry = pygit2.IndexEntry(path, blob_id, pygit2.GIT_FILEMODE_BLOB)
        index.add(entry)

    tree = index.write_tree()

    # Now apply a new commit
    author = pygit2.Signature('test', 'test@foo.bar')
    committer = pygit2.Signature('test', 'test@foo.bar')

    branch = git_repo.branches.get(branch_name)

    # Create commit and properly update branch and reflog
    oid = git_repo.create_commit(None, author, committer, '...', tree, [branch.target])
    commit = git_repo.get(oid)
    branch.set_target(commit.hex)

    # To make sure the serializer makes use of the new commit we'll have
    # to update the `git_hash` values on the version object.
    version.update(git_hash=commit.hex)


def test_temporary_worktree(settings):
    repo = AddonGitRepository(1)

    output = _run_process('git worktree list', repo)
    assert output.startswith(repo.git_repository_path)

    with TemporaryWorktree(repo.git_repository) as worktree:
        assert worktree.temp_directory.startswith(settings.TMP_PATH)
        assert worktree.path == os.path.join(worktree.temp_directory, worktree.name)

        output = _run_process('git worktree list', repo)
        assert worktree.name in output

    # Test that it cleans up properly
    assert not os.path.exists(worktree.temp_directory)
    output = _run_process('git worktree list', repo)
    assert worktree.name not in output


def test_enforce_pygit_global_search_path(settings):
    # Not using pygit2.option() here to make sure the call in
    # AddonGitRepository changes the correct things
    pygit2.settings.search_path[pygit2.GIT_CONFIG_LEVEL_GLOBAL] = '/root'

    assert pygit2.settings.search_path[pygit2.GIT_CONFIG_LEVEL_GLOBAL] == '/root'

    # Now initialize, which will overwrite the global setting.
    AddonGitRepository(1)

    assert pygit2.settings.search_path[pygit2.GIT_CONFIG_LEVEL_GLOBAL] == settings.ROOT


def test_git_repository_path():
    repo = AddonGitRepository(1789)
    assert repo.git_repository_path == os.path.join(
        settings.GIT_FILE_STORAGE_PATH, '89/1789/1789', 'addon'
    )


def test_git_repo_init(settings):
    repo = AddonGitRepository(1)
    assert repo.git_repository_path

    assert not os.path.exists(repo.git_repository_path)

    # accessing repo.git_repository creates the directory
    assert sorted(os.listdir(repo.git_repository.path)) == sorted(
        ['objects', 'refs', 'hooks', 'info', 'description', 'config', 'HEAD', 'logs']
    )
    assert repo.git_repository.lookup_reference('refs/heads/master')


def test_git_repo_init_with_missing_master_branch_raises_error(settings):
    repo = AddonGitRepository(1)
    # Create the git repo manually to avoid the creation of the master branch.
    pygit2.init_repository(path=repo.git_repository_path, bare=False)

    with pytest.raises(MissingMasterBranchError):
        repo.git_repository


def test_git_repo_init_opens_existing_repo(settings):
    expected_path = os.path.join(
        settings.GIT_FILE_STORAGE_PATH, '42/2342/4815162342', 'addon'
    )

    assert not os.path.exists(expected_path)

    repo = AddonGitRepository(4815162342)
    assert not os.path.exists(expected_path)

    assert repo.git_repository_path
    assert not os.path.exists(expected_path)

    # accessing repo.git_repository creates the directory
    repo.git_repository
    assert os.path.exists(expected_path)

    repo2 = AddonGitRepository(4815162342)
    assert repo.git_repository.path == repo2.git_repository.path


@pytest.mark.django_db
def test_extract_and_commit_from_version(settings):
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    repo = AddonGitRepository.extract_and_commit_from_version(addon.current_version)

    assert repo.git_repository_path == AddonGitRepository(addon.id).git_repository_path
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git branch', repo)
    assert 'listed' in output
    assert 'unlisted' not in output

    # Test that a new "unlisted" branch is created only if needed
    addon.current_version.update(channel=amo.CHANNEL_UNLISTED)
    repo = AddonGitRepository.extract_and_commit_from_version(
        version=addon.current_version
    )
    output = _run_process('git branch', repo)
    assert 'listed' in output
    assert 'unlisted' in output

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from {}'.format(
        repr(addon.current_version),
        addon.current_version.id,
        repr(addon),
        repr(addon.current_version.file),
    )
    assert expected in output

    # Check that the files are there.
    expected = {'extracted/README.md', 'extracted/manifest.json'}
    output = _run_process('git ls-tree -r --name-only listed', repo)
    assert set(output.split()) == expected


@pytest.mark.django_db
def test_extract_and_commit_from_version_set_git_hash():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    assert addon.current_version.git_hash == ''

    AddonGitRepository.extract_and_commit_from_version(version=addon.current_version)

    addon.current_version.refresh_from_db()
    assert len(addon.current_version.git_hash) == 40


@pytest.mark.django_db
def test_find_or_create_branch_raises_broken_ref_error(settings):
    repo = AddonGitRepository(
        addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})
    )
    branch = 'listed'
    # Create the git repo
    repo.git_repository
    assert repo.is_extracted
    # Create a broken ref, see:
    # https://github.com/mozilla/addons-server/issues/13590
    Path(f'{repo.git_repository_path}/.git/refs/heads/{branch}').touch()

    with pytest.raises(BrokenRefError):
        repo.find_or_create_branch(branch)


@pytest.mark.django_db
def test_delete(settings):
    addon = addon_factory(
        version_kw={'git_hash': 'some hash'},
        file_kw={'filename': 'webextension_no_id.xpi'},
    )
    # Create an unrelated add-on with a version.
    addon2 = addon_factory(
        version_kw={'git_hash': 'some hash'},
        file_kw={'filename': 'webextension_no_id.xpi'},
    )
    repo = AddonGitRepository(addon)
    # Create the git repo
    repo.git_repository
    assert repo.is_extracted
    assert addon.current_version.git_hash
    assert addon2.current_version.git_hash

    repo.delete()
    addon.refresh_from_db()
    addon2.refresh_from_db()

    assert not repo.is_extracted
    assert not addon.current_version.git_hash
    # The version of an unrelated add-on shouldn't be modified.
    assert addon2.current_version.git_hash


@pytest.mark.django_db
def test_delete_with_deleted_version(settings):
    addon = addon_factory(
        version_kw={'git_hash': 'some hash'},
        file_kw={'filename': 'webextension_no_id.xpi'},
    )
    version = addon.current_version
    version.delete()
    repo = AddonGitRepository(addon)
    # Create the git repo
    repo.git_repository
    assert repo.is_extracted
    assert version.git_hash

    repo.delete()
    version.refresh_from_db()

    assert not repo.is_extracted
    assert not version.git_hash


@pytest.mark.django_db
def test_delete_non_extracted_repo(settings):
    repo = AddonGitRepository(
        addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})
    )
    assert not repo.is_extracted

    repo.delete()

    assert not repo.is_extracted


@pytest.mark.django_db
def test_extract_and_commit_from_version_multiple_versions(settings):
    addon = addon_factory(
        file_kw={'filename': 'webextension_no_id.xpi'}, version_kw={'version': '0.1'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(addon.current_version)

    assert repo.git_repository_path == AddonGitRepository(addon.id).git_repository_path
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git branch', repo)
    assert 'listed' in output

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from {}'.format(
        repr(addon.current_version),
        addon.current_version.id,
        repr(addon),
        repr(addon.current_version.file),
    )
    assert expected in output

    # Create two more versions, check that they appear in the comitlog
    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}, version='0.2'
    )
    AddonGitRepository.extract_and_commit_from_version(version=version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}, version='0.3'
    )
    repo = AddonGitRepository.extract_and_commit_from_version(version=version)

    output = _run_process('git log listed', repo)
    assert output.count('Create new version') == 3
    assert '0.1' in output
    assert '0.2' in output
    assert '0.3' in output

    # 4 actual commits, including the repo initialization
    assert output.count('Mozilla Add-ons Robot') == 4

    # Make sure the commits didn't spill over into the master branch
    output = _run_process('git log', repo)
    assert output.count('Mozilla Add-ons Robot') == 1
    assert '0.1' not in output


@pytest.mark.django_db
def test_extract_and_commit_from_version_use_applied_author():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})
    user = user_factory(email='fancyuser@foo.bar', display_name='Fancy Test User')

    repo = AddonGitRepository.extract_and_commit_from_version(
        version=addon.current_version, author=user
    )

    output = _run_process('git log --format=full listed', repo)
    assert f'Author: User {user.id} <fancyuser@foo.bar>' in output
    assert (
        'Commit: Mozilla Add-ons Robot '
        '<addons-dev-automation+github@mozilla.com>' in output
    )


@pytest.mark.django_db
def test_extract_and_commit_from_version_use_addons_robot_default():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})
    repo = AddonGitRepository.extract_and_commit_from_version(
        version=addon.current_version
    )

    output = _run_process('git log --format=full listed', repo)
    assert (
        'Author: Mozilla Add-ons Robot '
        '<addons-dev-automation+github@mozilla.com>' in output
    )
    assert (
        'Commit: Mozilla Add-ons Robot '
        '<addons-dev-automation+github@mozilla.com>' in output
    )


@pytest.mark.django_db
def test_extract_and_commit_from_version_support_custom_note():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    repo = AddonGitRepository.extract_and_commit_from_version(
        version=addon.current_version, note='via signing'
    )

    output = _run_process('git log --format=full listed', repo)

    expected = 'Create new version {} ({}) for {} from {} (via signing)'.format(
        repr(addon.current_version),
        addon.current_version.id,
        repr(addon),
        repr(addon.current_version.file),
    )
    assert expected in output


@pytest.mark.django_db
@pytest.mark.parametrize(
    'filename',
    [
        'webextension_no_id.xpi',
        'webextension_no_id.zip',
    ],
)
def test_extract_and_commit_from_version_valid_extensions(settings, filename):
    addon = addon_factory(file_kw={'filename': filename})

    with mock.patch('olympia.files.utils.os.fsync') as fsync_mock:
        repo = AddonGitRepository.extract_and_commit_from_version(addon.current_version)

        # Make sure we are always calling fsync after extraction
        assert fsync_mock.called

    assert repo.git_repository_path == AddonGitRepository(addon.id).git_repository_path
    assert os.listdir(repo.git_repository_path) == ['.git']

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git branch', repo)
    assert 'listed' in output
    assert 'unlisted' not in output

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from {}'.format(
        repr(addon.current_version),
        addon.current_version.id,
        repr(addon),
        repr(addon.current_version.file),
    )
    assert expected in output


@pytest.mark.django_db
@pytest.mark.parametrize(
    'filename, expected',
    [
        ('webextension_no_id.xpi', {'README.md', 'manifest.json'}),
        ('webextension_no_id.zip', {'README.md', 'manifest.json'}),
        (
            'notify-link-clicks-i18n.xpi',
            {
                'README.md',
                '_locales/de/messages.json',
                '_locales/en/messages.json',
                '_locales/ja/messages.json',
                '_locales/nb_NO/messages.json',
                '_locales/nl/messages.json',
                '_locales/ru/messages.json',
                '_locales/sv/messages.json',
                'background-script.js',
                'content-script.js',
                'icons/LICENSE',
                'icons/link-48.png',
                'manifest.json',
            },
        ),
    ],
)
def test_extract_and_commit_from_version_commits_files(settings, filename, expected):
    addon = addon_factory(file_kw={'filename': filename})

    repo = AddonGitRepository.extract_and_commit_from_version(addon.current_version)

    # Verify via subprocess to make sure the repositories are properly
    # read by the regular git client
    output = _run_process('git ls-tree -r --name-only listed:extracted', repo)

    assert set(output.split()) == expected


@pytest.mark.django_db
def test_extract_and_commit_from_version_reverts_active_locale():
    from django.utils.translation import get_language

    addon = addon_factory(
        file_kw={'filename': 'webextension_no_id.xpi'}, version_kw={'version': '0.1'}
    )

    with activate_locale('fr'):
        repo = AddonGitRepository.extract_and_commit_from_version(addon.current_version)
        assert get_language() == 'fr'

    output = _run_process('git log listed', repo)
    expected = 'Create new version {} ({}) for {} from {}'.format(
        repr(addon.current_version),
        addon.current_version.id,
        repr(addon),
        repr(addon.current_version.file),
    )
    assert expected in output


@pytest.mark.django_db
def test_iter_tree():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    repo = AddonGitRepository.extract_and_commit_from_version(addon.current_version)

    commit = repo.git_repository.revparse_single('listed')

    tree = list(repo.iter_tree(repo.get_root_tree(commit)))

    # path, filename mapping
    expected_files = [
        ('README.md', 'README.md', _blob_type),
        ('_locales', '_locales', _tree_type),
        ('_locales/de', 'de', _tree_type),
        ('_locales/de/messages.json', 'messages.json', _blob_type),
        ('_locales/en', 'en', _tree_type),
        ('_locales/en/messages.json', 'messages.json', _blob_type),
        ('_locales/ja', 'ja', _tree_type),
        ('_locales/ja/messages.json', 'messages.json', _blob_type),
        ('_locales/nb_NO', 'nb_NO', _tree_type),
        ('_locales/nb_NO/messages.json', 'messages.json', _blob_type),
        ('_locales/nl', 'nl', _tree_type),
        ('_locales/nl/messages.json', 'messages.json', _blob_type),
        ('_locales/ru', 'ru', _tree_type),
        ('_locales/ru/messages.json', 'messages.json', _blob_type),
        ('_locales/sv', 'sv', _tree_type),
        ('_locales/sv/messages.json', 'messages.json', _blob_type),
        ('background-script.js', 'background-script.js', _blob_type),
        ('content-script.js', 'content-script.js', _blob_type),
        ('icons', 'icons', _tree_type),
        ('icons/LICENSE', 'LICENSE', _blob_type),
        ('icons/link-48.png', 'link-48.png', _blob_type),
        ('manifest.json', 'manifest.json', _blob_type),
    ]

    for idx, entry in enumerate(tree):
        expected_path, expected_name, expected_type = expected_files[idx]
        assert entry.path == expected_path
        assert entry.tree_entry.name == expected_name
        assert entry.tree_entry.type == expected_type


@pytest.mark.django_db
def test_get_diff_add_new_file():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'notify-link-clicks-i18n.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '{"id": "random"}\n', 'new_file.json')

    changes = repo.get_diff(commit=version.git_hash, parent=original_version.git_hash)

    assert changes[0]['hunks'] == [
        {
            'changes': [
                {
                    'content': '{"id": "random"}',
                    'new_line_number': 1,
                    'old_line_number': -1,
                    'type': 'insert',
                }
            ],
            'new_lines': 1,
            'new_start': 1,
            'old_lines': 0,
            'old_start': 0,
            'header': '@@ -0,0 +1 @@',
        }
    ]

    assert changes[0]['is_binary'] is False
    assert changes[0]['lines_added'] == 1
    assert changes[0]['lines_deleted'] == 0
    assert changes[0]['mode'] == 'A'
    assert changes[0]['old_path'] == 'new_file.json'
    assert changes[0]['path'] == 'new_file.json'
    assert changes[0]['size'] == 17
    assert changes[0]['parent'] == original_version.git_hash
    assert changes[0]['hash'] == version.git_hash


@pytest.mark.django_db
def test_get_diff_change_files():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'notify-link-clicks-i18n.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '{"id": "random"}\n', 'manifest.json')
    apply_changes(repo, version, 'Updated readme\n', 'README.md')

    changes = repo.get_diff(commit=version.git_hash, parent=original_version.git_hash)

    assert len(changes) == 2

    assert changes[0]
    assert changes[0]['is_binary'] is False
    assert changes[0]['lines_added'] == 1
    assert changes[0]['lines_deleted'] == 25
    assert changes[0]['mode'] == 'M'
    assert changes[0]['old_path'] == 'README.md'
    assert changes[0]['path'] == 'README.md'
    assert changes[0]['size'] == 15
    assert changes[0]['parent'] == original_version.git_hash
    assert changes[0]['hash'] == version.git_hash

    # There is actually just one big hunk in this diff since it's simply
    # removing everything and adding new content
    assert len(changes[0]['hunks']) == 1

    assert changes[0]['hunks'][0]['header'] == '@@ -1,25 +1 @@'
    assert changes[0]['hunks'][0]['old_start'] == 1
    assert changes[0]['hunks'][0]['new_start'] == 1
    assert changes[0]['hunks'][0]['old_lines'] == 25
    assert changes[0]['hunks'][0]['new_lines'] == 1

    hunk_changes = changes[0]['hunks'][0]['changes']

    assert hunk_changes[0] == {
        'content': '# notify-link-clicks-i18n',
        'new_line_number': -1,
        'old_line_number': 1,
        'type': 'delete',
    }

    assert all(x['type'] == 'delete' for x in hunk_changes[:-1])

    assert hunk_changes[-1] == {
        'content': 'Updated readme',
        'new_line_number': 1,
        'old_line_number': -1,
        'type': 'insert',
    }

    assert changes[1]
    assert changes[1]['is_binary'] is False
    assert changes[1]['lines_added'] == 1
    assert changes[1]['lines_deleted'] == 32
    assert changes[1]['mode'] == 'M'
    assert changes[1]['old_path'] == 'manifest.json'
    assert changes[1]['path'] == 'manifest.json'
    assert changes[1]['size'] == 17
    assert changes[1]['parent'] == original_version.git_hash
    assert changes[1]['hash'] == version.git_hash

    # There is actually just one big hunk in this diff since it's simply
    # removing everything and adding new content
    assert len(changes[1]['hunks']) == 1

    assert changes[1]['hunks'][0]['header'] == '@@ -1,32 +1 @@'
    assert changes[1]['hunks'][0]['old_start'] == 1
    assert changes[1]['hunks'][0]['new_start'] == 1
    assert changes[1]['hunks'][0]['old_lines'] == 32
    assert changes[1]['hunks'][0]['new_lines'] == 1

    hunk_changes = changes[1]['hunks'][0]['changes']

    assert all(x['type'] == 'delete' for x in hunk_changes[:-1])

    assert hunk_changes[-1] == {
        'content': '{"id": "random"}',
        'new_line_number': 1,
        'old_line_number': -1,
        'type': 'insert',
    }


@pytest.mark.django_db
def test_get_diff_initial_commit():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    version = addon.current_version
    repo = AddonGitRepository.extract_and_commit_from_version(version)

    changes = repo.get_diff(commit=version.git_hash, parent=None)

    # This makes sure that sub-directories are diffed properly too
    assert changes[1]['is_binary'] is False
    assert changes[1]['lines_added'] == 27
    assert changes[1]['lines_deleted'] == 0
    assert changes[1]['mode'] == 'A'
    assert changes[1]['old_path'] == '_locales/de/messages.json'
    assert changes[1]['parent'] == version.git_hash
    assert changes[1]['hash'] == version.git_hash
    assert changes[1]['path'] == '_locales/de/messages.json'
    assert changes[1]['size'] == 658

    # It's all an insert
    assert all(x['type'] == 'insert' for x in changes[1]['hunks'][0]['changes'])

    assert changes[-1]['is_binary'] is False
    assert changes[-1]['lines_added'] == 32
    assert changes[-1]['lines_deleted'] == 0
    assert changes[-1]['mode'] == 'A'
    assert changes[-1]['old_path'] == 'manifest.json'
    assert changes[-1]['parent'] == version.git_hash
    assert changes[-1]['hash'] == version.git_hash
    assert changes[-1]['path'] == 'manifest.json'
    assert changes[-1]['size'] == 622

    # Binary files work fine too
    assert changes[-2]['hash'] == version.git_hash
    assert changes[-2]['parent'] == version.git_hash
    assert changes[-2]['hunks'] == []
    assert changes[-2]['is_binary'] is True
    assert changes[-2]['lines_added'] == 0
    assert changes[-2]['lines_deleted'] == 0
    assert changes[-2]['mode'] == 'A'
    assert changes[-2]['old_path'] == 'icons/link-48.png'
    assert changes[-2]['path'] == 'icons/link-48.png'
    assert changes[-2]['size'] == 596


@pytest.mark.django_db
def test_get_diff_initial_commit_pathspec():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    version = addon.current_version
    repo = AddonGitRepository.extract_and_commit_from_version(version)

    changes = repo.get_diff(
        commit=version.git_hash, parent=None, pathspec=['_locales/de/messages.json']
    )

    assert len(changes) == 1
    # This makes sure that sub-directories are diffed properly too
    assert changes[0]['is_binary'] is False
    assert changes[0]['lines_added'] == 27
    assert changes[0]['lines_deleted'] == 0
    assert changes[0]['mode'] == 'A'
    assert changes[0]['old_path'] == '_locales/de/messages.json'
    assert changes[0]['parent'] == version.git_hash
    assert changes[0]['hash'] == version.git_hash
    assert changes[0]['path'] == '_locales/de/messages.json'
    assert changes[0]['size'] == 658

    # It's all an insert
    assert all(x['type'] == 'insert' for x in changes[0]['hunks'][0]['changes'])


@pytest.mark.django_db
def test_get_diff_change_files_pathspec():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'notify-link-clicks-i18n.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '{"id": "random"}\n', 'manifest.json')
    apply_changes(repo, version, 'Updated readme\n', 'README.md')

    changes = repo.get_diff(
        commit=version.git_hash,
        parent=original_version.git_hash,
        pathspec=['README.md'],
    )

    assert len(changes) == 1

    assert changes[0]
    assert changes[0]['is_binary'] is False
    assert changes[0]['lines_added'] == 1
    assert changes[0]['lines_deleted'] == 25
    assert changes[0]['mode'] == 'M'
    assert changes[0]['old_path'] == 'README.md'
    assert changes[0]['path'] == 'README.md'
    assert changes[0]['size'] == 15
    assert changes[0]['parent'] == original_version.git_hash
    assert changes[0]['hash'] == version.git_hash

    # There is actually just one big hunk in this diff since it's simply
    # removing everything and adding new content
    assert len(changes[0]['hunks']) == 1

    assert changes[0]['hunks'][0]['header'] == '@@ -1,25 +1 @@'
    assert changes[0]['hunks'][0]['old_start'] == 1
    assert changes[0]['hunks'][0]['new_start'] == 1
    assert changes[0]['hunks'][0]['old_lines'] == 25
    assert changes[0]['hunks'][0]['new_lines'] == 1

    hunk_changes = changes[0]['hunks'][0]['changes']

    assert hunk_changes[0] == {
        'content': '# notify-link-clicks-i18n',
        'new_line_number': -1,
        'old_line_number': 1,
        'type': 'delete',
    }

    assert all(x['type'] == 'delete' for x in hunk_changes[:-1])

    assert hunk_changes[-1] == {
        'content': 'Updated readme',
        'new_line_number': 1,
        'old_line_number': -1,
        'type': 'insert',
    }


@pytest.mark.django_db
def test_get_diff_newline_old_file():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '{"id": "random"}', 'manifest.json')

    changes = repo.get_diff(commit=version.git_hash, parent=original_version.git_hash)

    assert len(changes) == 1
    assert changes[0]['new_ending_new_line'] is False
    assert changes[0]['old_ending_new_line'] is True

    hunk_changes = changes[0]['hunks'][0]['changes']

    assert hunk_changes[-1] == {
        'content': '\n\\ No newline at end of file',
        'type': 'delete-eofnl',
        'old_line_number': -1,
        'new_line_number': 1,
    }


@pytest.mark.django_db
def test_get_diff_newline_new_file():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    parent_version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(parent_version)

    # Let's remove the newline
    apply_changes(repo, parent_version, '{"id": "random"}', 'manifest.json')

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    # Now we're adding it again
    apply_changes(repo, version, '{"id": "random"}\n', 'manifest.json')

    changes = repo.get_diff(commit=version.git_hash, parent=parent_version.git_hash)

    # The file has been modified, so as far as git is concerned there should
    # be one change.
    assert len(changes) == 1
    assert changes[0]['mode'] == 'M'
    assert changes[0]['new_ending_new_line'] is True
    assert changes[0]['old_ending_new_line'] is True

    # We are ignoring all whitespace modifications so there is no diff.
    assert not changes[0]['hunks']


@pytest.mark.django_db
def test_get_diff_eol_changes():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    parent_version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(parent_version)

    # Let's replace the unix eol with dos ones.
    apply_changes(
        repo, parent_version, '{"id": "random",\r\n"name": "foo"\r\n}', 'manifest.json'
    )

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    # Now go back to unix eol.
    apply_changes(repo, version, '{"id": "random",\n"name": "foo"\n}', 'manifest.json')

    changes = repo.get_diff(commit=version.git_hash, parent=parent_version.git_hash)

    # The file has been modified, so as far as git is concerned there should
    # be one change.
    assert len(changes) == 1
    assert changes[0]['mode'] == 'M'

    # We are ignoring all whitespace modifications so there is no diff.
    assert not changes[0]['hunks']


@pytest.mark.django_db
def test_get_diff_whitespace_changes_only():
    file_kw = {'filename': 'webextension_no_id.xpi'}
    addon = addon_factory(file_kw=file_kw)

    original_version = addon.current_version
    AddonGitRepository.extract_and_commit_from_version(original_version)

    parent_version = version_factory(addon=addon, file_kw=file_kw)
    repo = AddonGitRepository.extract_and_commit_from_version(parent_version)
    apply_changes(repo, parent_version, '# beastify\r', 'README.md')

    version = version_factory(addon=addon, file_kw=file_kw)
    repo = AddonGitRepository.extract_and_commit_from_version(version)
    apply_changes(repo, version, '# beastify\r\n', 'README.md')

    changes = repo.get_diff(
        commit=version.git_hash, parent=parent_version.git_hash, pathspec=['README.md']
    )

    # The file has been modified, so as far as git is concerned there should be
    # one change.
    assert len(changes) == 1
    assert changes[0]['mode'] == 'M'

    # If the entire file has whitespace changes only, there should be no hunks,
    # which is problematic because it would result in an empty content and we
    # don't want that so we handle this special case.
    # See: https://github.com/mozilla/addons-server/issues/15966
    assert changes[0]['hunks']
    assert changes[0]['hunks'][0]['changes'][0]['content'] == b'# beastify\r'


@pytest.mark.django_db
def test_get_diff_newline_both_no_newline():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    parent_version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(parent_version)

    # Let's remove the newline
    apply_changes(repo, parent_version, '{"id": "random"}', 'manifest.json')

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    # And create another change that doesn't add a newline
    apply_changes(
        repo, version, '{"id": "new random id",\n"something": "foo"}', 'manifest.json'
    )

    changes = repo.get_diff(commit=version.git_hash, parent=parent_version.git_hash)

    assert len(changes) == 1
    assert changes[0]['new_ending_new_line'] is False
    assert changes[0]['old_ending_new_line'] is False

    hunk_changes = changes[0]['hunks'][0]['changes']

    # The following structure represents a diff similar to this one
    #
    # diff --git a/manifest.json b/manifest.json
    # index 72bd4f0..1f666c8 100644
    # --- a/manifest.json
    # +++ b/manifest.json
    # @@ -1 +1,2 @@
    # -{"id": "random"}
    # \ No newline at end of file
    # +{"id": "new random id",
    # +"something": "foo"}
    # \ No newline at end of file
    assert hunk_changes == [
        {
            'content': '{"id": "random"}',
            'new_line_number': -1,
            'old_line_number': 1,
            'type': 'delete',
        },
        {
            'content': '\n\\ No newline at end of file',
            'new_line_number': -1,
            'old_line_number': 1,
            'type': 'insert-eofnl',
        },
        {
            'content': '{"id": "new random id",',
            'new_line_number': 1,
            'old_line_number': -1,
            'type': 'insert',
        },
        {
            'content': '"something": "foo"}',
            'new_line_number': 2,
            'old_line_number': -1,
            'type': 'insert',
        },
        {
            'content': '\n\\ No newline at end of file',
            'new_line_number': 2,
            'old_line_number': -1,
            'type': 'delete-eofnl',
        },
    ]


@pytest.mark.django_db
def test_get_diff_delete_file():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '', 'manifest.json', delete=True)

    changes = repo.get_diff(commit=version.git_hash, parent=original_version.git_hash)

    assert changes[0]['mode'] == 'D'
    assert all(x['type'] == 'delete' for x in changes[0]['hunks'][0]['changes'])


@pytest.mark.django_db
def test_get_diff_unmodified_file_by_default_not_rendered():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    changes = repo.get_diff(commit=version.git_hash, parent=original_version.git_hash)

    assert not changes


@pytest.mark.django_db
def test_get_diff_unmodified_file():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    changes = repo.get_diff(
        commit=version.git_hash,
        parent=original_version.git_hash,
        pathspec=['manifest.json'],
    )

    assert len(changes) == 1
    assert changes[0]['mode'] == ' '
    assert changes[0]['hunks'][0]['header'] == '@@ -0 +0 @@'
    assert all(x['type'] == 'normal' for x in changes[0]['hunks'][0]['changes'])

    # Make sure line numbers start at 1
    # https://github.com/mozilla/addons-server/issues/11739
    assert changes[0]['hunks'][0]['changes'][0]['new_line_number'] == 1
    assert changes[0]['hunks'][0]['changes'][0]['old_line_number'] == 1


@pytest.mark.django_db
def test_get_diff_unmodified_file_binary_file():
    addon = addon_factory(file_kw={'filename': 'https-everywhere.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(addon=addon, file_kw={'filename': 'https-everywhere.xpi'})

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    changes = repo.get_diff(
        commit=version.git_hash,
        parent=original_version.git_hash,
        pathspec=['manifest.json'],
    )

    assert len(changes) == 1
    assert changes[0]['mode'] == ' '
    assert changes[0]['hunks'][0]['header'] == '@@ -0 +0 @@'
    assert all(x['type'] == 'normal' for x in changes[0]['hunks'][0]['changes'])

    # Now Make sure we don't render a fake hunk for binary files such as images
    changes = repo.get_diff(
        commit=version.git_hash,
        parent=original_version.git_hash,
        pathspec=['icon16.png'],
    )

    assert len(changes) == 1
    assert not changes[0]['hunks']


@pytest.mark.django_db
def test_get_raw_diff_cache():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '', 'manifest.json', delete=True)

    with mock.patch('olympia.git.utils.pygit2.Repository.diff') as mocked_diff:
        repo.get_diff(commit=version.git_hash, parent=original_version.git_hash)

        repo.get_diff(commit=version.git_hash, parent=original_version.git_hash)

        mocked_diff.assert_called_once()

    assert list(repo._diff_cache.keys()) == [
        (version.git_hash, original_version.git_hash, False),
    ]


@pytest.mark.django_db
def test_get_raw_diff_cache_unmodified_file():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    with mock.patch('olympia.git.utils.pygit2.Repository.diff') as mocked_diff:
        repo.get_diff(commit=version.git_hash, parent=original_version.git_hash)

        repo.get_diff(
            commit=version.git_hash,
            parent=original_version.git_hash,
            pathspec=['manifest.json'],
        )

        assert mocked_diff.call_count == 2

    assert list(repo._diff_cache.keys()) == [
        # `get_diff` call without pathspec, not rendering unmodified files
        (version.git_hash, original_version.git_hash, False),
        # `get_diff` call with pathspec, rendering unmodified files
        (version.git_hash, original_version.git_hash, True),
    ]


@pytest.mark.parametrize(
    'entry, filename, expected_category, expected_mimetype',
    [
        (MagicMock(type=_blob_type), 'blank.pdf', 'binary', 'application/pdf'),
        (MagicMock(type=_blob_type), 'blank.txt', 'text', 'text/plain'),
        (
            MagicMock(type=_blob_type),
            'empty_bat.exe',
            'binary',
            'application/x-msdos-program',
        ),
        (MagicMock(type=_blob_type), 'fff.gif', 'image', 'image/gif'),
        (MagicMock(type=_blob_type), 'foo.css', 'text', 'text/css'),
        (MagicMock(type=_blob_type), 'foo.html', 'text', 'text/html'),
        (MagicMock(type=_blob_type), 'foo.js', 'text', 'text/javascript'),
        (MagicMock(type=_blob_type), 'foo.py', 'text', 'text/x-python'),
        (MagicMock(type=_blob_type), 'image.jpg', 'image', 'image/jpeg'),
        (MagicMock(type=_blob_type), 'image.png', 'image', 'image/png'),
        (MagicMock(type=_blob_type), 'foo.xml', 'text', 'application/xml'),
        (
            MagicMock(type=_blob_type),
            'js_containing_png_data.js',
            'text',
            'text/javascript',
        ),
        (MagicMock(type=_blob_type), 'foo.json', 'text', 'application/json'),
        (MagicMock(type=_tree_type), 'foo', 'directory', 'application/octet-stream'),
        (
            MagicMock(type=_blob_type),
            'image-svg-without-xml.svg',
            'image',
            'image/svg+xml',
        ),
        (MagicMock(type=_blob_type), 'bmp-v3.bmp', 'image', 'image/bmp'),
        (MagicMock(type=_blob_type), 'bmp-v4.bmp', 'image', 'image/bmp'),
        (MagicMock(type=_blob_type), 'bmp-v5.bmp', 'image', 'image/bmp'),
        (MagicMock(type=_blob_type), 'bmp-os2-v1.bmp', 'image', 'image/bmp'),
        (MagicMock(type=_blob_type), 'foo.ts', 'text', 'text/javascript'),
    ],
)
def test_get_mime_type_for_blob(entry, filename, expected_category, expected_mimetype):
    mime, category = get_mime_type_for_blob(entry.type, filename)

    assert mime == expected_mimetype
    assert category == expected_category


@pytest.mark.django_db
def test_get_deltas_add_new_file():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'notify-link-clicks-i18n.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '{"id": "random"}\n', 'new_file.json')

    changes = repo.get_deltas(commit=version.git_hash, parent=original_version.git_hash)

    assert changes[0]['mode'] == 'A'
    assert changes[0]['old_path'] == 'new_file.json'
    assert changes[0]['path'] == 'new_file.json'
    assert changes[0]['parent'] == original_version.git_hash
    assert changes[0]['hash'] == version.git_hash


@pytest.mark.django_db
def test_get_deltas_change_files():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'notify-link-clicks-i18n.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '{"id": "random"}\n', 'manifest.json')
    apply_changes(repo, version, 'Updated readme\n', 'README.md')

    changes = repo.get_deltas(commit=version.git_hash, parent=original_version.git_hash)

    assert len(changes) == 2

    assert changes[0]
    assert changes[0]['mode'] == 'M'
    assert changes[0]['old_path'] == 'README.md'
    assert changes[0]['path'] == 'README.md'
    assert changes[0]['parent'] == original_version.git_hash
    assert changes[0]['hash'] == version.git_hash


@pytest.mark.django_db
def test_get_deltas_initial_commit():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    version = addon.current_version
    repo = AddonGitRepository.extract_and_commit_from_version(version)

    changes = repo.get_deltas(commit=version.git_hash, parent=None)

    # This makes sure that sub-directories are diffed properly too
    assert changes[1]['mode'] == 'A'
    assert changes[1]['old_path'] == '_locales/de/messages.json'
    assert changes[1]['parent'] == version.git_hash
    assert changes[1]['hash'] == version.git_hash
    assert changes[1]['path'] == '_locales/de/messages.json'


@pytest.mark.django_db
def test_get_deltas_initial_commit_pathspec():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    version = addon.current_version
    repo = AddonGitRepository.extract_and_commit_from_version(version)

    changes = repo.get_deltas(
        commit=version.git_hash, parent=None, pathspec=['_locales/de/messages.json']
    )

    assert len(changes) == 1

    # This makes sure that sub-directories are diffed properly too
    assert changes[0]['mode'] == 'A'
    assert changes[0]['old_path'] == '_locales/de/messages.json'
    assert changes[0]['parent'] == version.git_hash
    assert changes[0]['hash'] == version.git_hash
    assert changes[0]['path'] == '_locales/de/messages.json'


@pytest.mark.django_db
def test_get_deltas_change_files_pathspec():
    addon = addon_factory(file_kw={'filename': 'notify-link-clicks-i18n.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'notify-link-clicks-i18n.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '{"id": "random"}\n', 'manifest.json')
    apply_changes(repo, version, 'Updated readme\n', 'README.md')

    changes = repo.get_deltas(
        commit=version.git_hash,
        parent=original_version.git_hash,
        pathspec=['README.md'],
    )

    assert len(changes) == 1

    assert changes[0]
    assert changes[0]['mode'] == 'M'
    assert changes[0]['old_path'] == 'README.md'
    assert changes[0]['path'] == 'README.md'
    assert changes[0]['parent'] == original_version.git_hash
    assert changes[0]['hash'] == version.git_hash


@pytest.mark.django_db
def test_get_deltas_delete_file():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    apply_changes(repo, version, '', 'manifest.json', delete=True)

    changes = repo.get_deltas(commit=version.git_hash, parent=original_version.git_hash)

    assert changes[0]['mode'] == 'D'


@pytest.mark.django_db
def test_get_deltas_unmodified_file_by_default_not_rendered():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    original_version = addon.current_version

    AddonGitRepository.extract_and_commit_from_version(original_version)

    version = version_factory(
        addon=addon, file_kw={'filename': 'webextension_no_id.xpi'}
    )

    repo = AddonGitRepository.extract_and_commit_from_version(version)

    changes = repo.get_deltas(commit=version.git_hash, parent=original_version.git_hash)

    assert not changes


# See: https://github.com/mozilla/addons-server/issues/13932
@pytest.mark.django_db
def test_commit_with_old_reference(settings):
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})
    repo = AddonGitRepository(addon.pk)
    branch_1 = repo.find_or_create_branch('branch-1')
    # Save the reference to 'branch-2' for later.
    branch_2 = repo.find_or_create_branch('branch-2')

    # We create an empyy commit on the 'branch-1'.
    commit = repo._commit_through_worktree(
        file_obj=None, message='commit on branch 1', author=None, branch=branch_1
    )
    # We set the target of the most up-to-date reference to 'branch 2'. This
    # means both branches point to the same commit created above.
    repo.find_or_create_branch('branch-2').set_target(commit.hex)
    # We create a second commit on the second branch and we pass the old
    # reference to 'branch-2'.
    repo._commit_through_worktree(
        file_obj=None, message='commit on branch 2', author=None, branch=branch_2
    )

    # `git log` but with messages only
    output = _run_process('git log --format=format:%s branch-2', repo)
    assert output == '\n'.join(
        [
            'commit on branch 2',
            'commit on branch 1',
            'Initializing repository',  # created by default
        ]
    )


def test_is_recent_is_false_when_not_extracted(settings):
    addon_pk = 123
    repo = AddonGitRepository(addon_pk)

    assert not repo.is_recent


def test_is_recent(settings):
    addon_pk = 123
    repo = AddonGitRepository(addon_pk)
    # Force the creation of the git repository.
    repo.git_repository

    assert repo.is_recent


def test_is_recent_with_no_description_file(settings):
    addon_pk = 123
    repo = AddonGitRepository(addon_pk)
    # Force the creation of the git repository.
    repo.git_repository
    # We should have a description file but we noticed errors about missing
    # files in production so let's protect against those errors.
    Path(os.path.join(repo.git_repository_path, repo.GIT_DESCRIPTION)).unlink()

    assert not repo.is_recent


def test_is_recent_with_relatively_recent_repo(settings):
    addon_pk = 123
    repo = AddonGitRepository(addon_pk)
    # Force the creation of the git repository.
    repo.git_repository
    fifteen_min_ago = datetime.datetime.now() - datetime.timedelta(minutes=15)
    update_git_repo_creation_time(repo, time=fifteen_min_ago)

    assert repo.is_recent


def test_is_recent_with_very_old_repo(settings):
    addon_pk = 123
    repo = AddonGitRepository(addon_pk)
    # Force the creation of the git repository.
    repo.git_repository
    update_git_repo_creation_time(repo, time=datetime.datetime(2020, 1, 1))

    assert not repo.is_recent


def test_is_recent_with_oldish_repo(settings):
    addon_pk = 123
    repo = AddonGitRepository(addon_pk)
    # Force the creation of the git repository.
    repo.git_repository
    time = datetime.datetime.now() - datetime.timedelta(minutes=60)
    update_git_repo_creation_time(repo, time=time)

    assert not repo.is_recent


@pytest.mark.django_db
@mock.patch('olympia.git.utils.statsd.timer')
@mock.patch('olympia.git.utils.statsd.incr')
def test_extract_version_to_git(incr_mock, timer_mock):
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    extract_version_to_git(addon.current_version.pk)

    repo = AddonGitRepository(addon.pk)

    assert os.listdir(repo.git_repository_path) == ['.git']
    timer_mock.assert_any_call('git.extraction.version')
    incr_mock.assert_called_with('git.extraction.version.success')


@pytest.mark.django_db
def test_extract_version_to_git_deleted_version():
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})

    version = addon.current_version
    version.delete()

    extract_version_to_git(version.pk)

    repo = AddonGitRepository(addon.pk)

    assert os.listdir(repo.git_repository_path) == ['.git']


@pytest.mark.django_db
def test_extract_version_to_git_with_not_extension_type():
    addon = addon_factory(type=amo.ADDON_STATICTHEME)

    extract_version_to_git(addon.current_version.pk)

    repo = AddonGitRepository(addon.pk)
    assert not repo.is_extracted


@pytest.mark.django_db
@mock.patch('olympia.git.utils.AddonGitRepository.extract_and_commit_from_version')
@mock.patch('olympia.git.utils.statsd.incr')
def test_extract_version_to_git_with_error(incr_mock, extract_and_commit_mock):
    addon = addon_factory(file_kw={'filename': 'webextension_no_id.xpi'})
    extract_and_commit_mock.side_effect = Exception()

    with pytest.raises(Exception):
        extract_version_to_git(addon.current_version.pk)

    incr_mock.assert_called_with('git.extraction.version.failure')


@pytest.mark.django_db
def test_extract_and_commit_no_dotgit_clash(settings):
    addon = addon_factory(file_kw={'filename': 'webextension_dotgit.xpi'})

    with mock.patch('olympia.git.utils.uuid.uuid4') as uuid4_mock:
        uuid4_mock.return_value = mock.Mock(hex='b236f5994773477bbcd2d1b75ab1458f')
        repo = AddonGitRepository.extract_and_commit_from_version(addon.current_version)

    assert os.listdir(repo.git_repository_path) == ['.git']
    # Verify via subprocess to make sure the repositories are properly read by
    # the regular git client.
    output = _run_process('git ls-tree -r --name-only listed', repo)
    assert set(output.split()) == {
        'extracted/manifest.json',
        'extracted/.git.b236f599/config',
    }


@pytest.mark.django_db
def test_extract_and_commit_source_from_version_rename_dotgit_files(settings):
    addon = addon_factory(file_kw={'filename': 'webextension_dotgit_2.xpi'})

    with mock.patch('olympia.git.utils.uuid.uuid4') as uuid4_mock:
        uuid4_mock.return_value = mock.Mock(hex='b236f5994773477bbcd2d1b75ab1458f')
        repo = AddonGitRepository.extract_and_commit_from_version(addon.current_version)

    # Verify via subprocess to make sure the repositories are properly read by
    # the regular git client.
    output = _run_process('git ls-tree -r --name-only listed', repo)
    assert set(output.split()) == {
        'extracted/.git.b236f599/.git.b236f599/.git.b236f599/.gitconfig.b236f599',  # noqa
        'extracted/.git.b236f599/.gitmodules.b236f599',
        'extracted/.gitattributes.b236f599',
        'extracted/.gitignore.b236f599',
        'extracted/.gitmodules.b236f599',
        'extracted/manifest.json',
        'extracted/some/directory/.gitattributes.b236f599',
        'extracted/some/directory/.gitignore.b236f599',
        'extracted/some/directory/.gitmodules.b236f599',
    }
