from datetime import datetime, timedelta
from unittest import mock

import pytest

from olympia import amo
from olympia.amo.tests import (
    TestCase,
    addon_factory,
    user_factory,
    version_factory,
    version_review_flags_factory,
)
from olympia.constants.scanners import (
    CUSTOMS,
    DELAY_AUTO_APPROVAL,
    DELAY_AUTO_APPROVAL_INDEFINITELY,
    FLAG_FOR_HUMAN_REVIEW,
    MAD,
    NO_ACTION,
    YARA,
)
from olympia.files.models import FileUpload
from olympia.scanners.models import (
    ScannerResult,
    ScannerRule,
)
from olympia.scanners.actions import (
    _delay_auto_approval,
    _delay_auto_approval_indefinitely,
    _delay_auto_approval_indefinitely_and_restrict,
    _delay_auto_approval_indefinitely_and_restrict_future_approvals,
    _flag_for_human_review,
    _flag_for_human_review_by_scanner,
    _no_action,
)
from olympia.users.models import (
    EmailUserRestriction,
    IPNetworkUserRestriction,
    RESTRICTION_TYPES,
)
from olympia.versions.models import VersionReviewerFlags


class TestActions(TestCase):
    def test_action_does_nothing(self):
        version = version_factory(addon=addon_factory())
        _no_action(version)

    def test_flags_a_version_for_human_review(self):
        version = version_factory(addon=addon_factory())
        assert not version.needs_human_review
        _flag_for_human_review(version)
        assert version.needs_human_review
        version.reload()
        assert version.needs_human_review

    def test_delay_auto_approval(self):
        addon = addon_factory()
        version = addon.current_version
        assert not version.needs_human_review
        assert addon.auto_approval_delayed_until is None
        _delay_auto_approval(version)
        self.assertCloseToNow(
            addon.auto_approval_delayed_until,
            now=datetime.now() + timedelta(hours=24),
        )
        assert version.needs_human_review

    def test_delay_auto_approval_indefinitely(self):
        addon = addon_factory()
        version = addon.current_version
        assert not version.needs_human_review
        assert addon.auto_approval_delayed_until is None
        _delay_auto_approval_indefinitely(version)
        assert addon.auto_approval_delayed_until == datetime.max
        assert version.needs_human_review

    def test_delay_auto_approval_indefinitely_and_restrict(self):
        user1 = user_factory(last_login_ip='5.6.7.8')
        user2 = user_factory(last_login_ip='')
        user3 = user_factory()
        user4 = user_factory(last_login_ip='4.8.15.16')
        addon = addon_factory(users=[user1, user2])
        FileUpload.objects.create(
            addon=addon,
            user=user3,
            version=addon.current_version.version,
            ip_address='1.2.3.4',
            source=amo.UPLOAD_SOURCE_DEVHUB,
            channel=amo.CHANNEL_LISTED,
        )
        version = addon.current_version
        assert not version.needs_human_review
        assert addon.auto_approval_delayed_until is None
        _delay_auto_approval_indefinitely_and_restrict(version)
        assert addon.auto_approval_delayed_until == datetime.max
        assert version.needs_human_review
        assert EmailUserRestriction.objects.filter(
            email_pattern=user1.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user2.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user3.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert not EmailUserRestriction.objects.filter(
            email_pattern=user4.email
        ).exists()

        assert IPNetworkUserRestriction.objects.filter(
            network='5.6.7.8/32', restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert IPNetworkUserRestriction.objects.filter(
            network='1.2.3.4/32', restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert not IPNetworkUserRestriction.objects.filter(network=None).exists()
        assert not IPNetworkUserRestriction.objects.filter(network='').exists()
        assert not IPNetworkUserRestriction.objects.filter(
            restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert not EmailUserRestriction.objects.filter(
            restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()

    def test_delay_auto_approval_indefinitely_and_restrict_already_restricted(self):
        user1 = user_factory(last_login_ip='5.6.7.8')
        user2 = user_factory(last_login_ip='')
        user3 = user_factory()
        user4 = user_factory(last_login_ip='4.8.15.16')
        EmailUserRestriction.objects.create(email_pattern=user1.email)
        EmailUserRestriction.objects.create(email_pattern=user3.email)
        IPNetworkUserRestriction.objects.create(network='5.6.7.8/32')
        addon = addon_factory(users=[user1, user2])
        FileUpload.objects.create(
            addon=addon,
            user=user3,
            version=addon.current_version.version,
            ip_address='1.2.3.4',
            source=amo.UPLOAD_SOURCE_DEVHUB,
            channel=amo.CHANNEL_LISTED,
        )
        version = addon.current_version
        assert not version.needs_human_review
        assert addon.auto_approval_delayed_until is None
        _delay_auto_approval_indefinitely_and_restrict(version)
        assert addon.auto_approval_delayed_until == datetime.max
        assert version.needs_human_review
        assert EmailUserRestriction.objects.filter(
            email_pattern=user1.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user2.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user3.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert not EmailUserRestriction.objects.filter(
            email_pattern=user4.email
        ).exists()

        assert IPNetworkUserRestriction.objects.filter(
            network='5.6.7.8/32', restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert IPNetworkUserRestriction.objects.filter(
            network='1.2.3.4/32', restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert not IPNetworkUserRestriction.objects.filter(network=None).exists()
        assert not IPNetworkUserRestriction.objects.filter(network='').exists()

    def test_delay_auto_approval_indefinitely_and_restrict_already_restricted_other(
        self,
    ):
        user1 = user_factory(last_login_ip='5.6.7.8')
        user2 = user_factory(last_login_ip='')
        user3 = user_factory()
        user4 = user_factory(last_login_ip='4.8.15.16')
        EmailUserRestriction.objects.create(
            email_pattern=user1.email, restriction_type=RESTRICTION_TYPES.APPROVAL
        )
        EmailUserRestriction.objects.create(
            email_pattern=user3.email, restriction_type=RESTRICTION_TYPES.APPROVAL
        )
        IPNetworkUserRestriction.objects.create(
            network='5.6.7.8/32', restriction_type=RESTRICTION_TYPES.APPROVAL
        )
        addon = addon_factory(users=[user1, user2])
        FileUpload.objects.create(
            addon=addon,
            user=user3,
            version=addon.current_version.version,
            ip_address='1.2.3.4',
            source=amo.UPLOAD_SOURCE_DEVHUB,
            channel=amo.CHANNEL_LISTED,
        )
        version = addon.current_version
        assert not version.needs_human_review
        assert addon.auto_approval_delayed_until is None
        _delay_auto_approval_indefinitely_and_restrict(version)
        assert addon.auto_approval_delayed_until == datetime.max
        assert version.needs_human_review
        # We added a new restriction for submission without touching the existing one
        # for approval for user1 and user3
        assert EmailUserRestriction.objects.filter(
            email_pattern=user1.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user1.email, restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user2.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user3.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user3.email, restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert not EmailUserRestriction.objects.filter(
            email_pattern=user4.email
        ).exists()

        # Like above, we added a new restriction for submission, this time for the ip,
        # but we left the one for approval.
        assert IPNetworkUserRestriction.objects.filter(
            network='5.6.7.8/32', restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert IPNetworkUserRestriction.objects.filter(
            network='5.6.7.8/32', restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert IPNetworkUserRestriction.objects.filter(
            network='1.2.3.4/32', restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert not IPNetworkUserRestriction.objects.filter(network=None).exists()
        assert not IPNetworkUserRestriction.objects.filter(network='').exists()

    def test_delay_auto_approval_indefinitely_and_restrict_future_approvals(self):
        user1 = user_factory(last_login_ip='5.6.7.8')
        user2 = user_factory(last_login_ip='')
        user3 = user_factory()
        user4 = user_factory(last_login_ip='4.8.15.16')
        EmailUserRestriction.objects.create(
            email_pattern=user1.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        )
        EmailUserRestriction.objects.create(
            email_pattern=user3.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        )
        IPNetworkUserRestriction.objects.create(
            network='5.6.7.8/32', restriction_type=RESTRICTION_TYPES.SUBMISSION
        )
        addon = addon_factory(users=[user1, user2])
        FileUpload.objects.create(
            addon=addon,
            user=user3,
            version=addon.current_version.version,
            ip_address='1.2.3.4',
            source=amo.UPLOAD_SOURCE_DEVHUB,
            channel=amo.CHANNEL_LISTED,
        )
        version = addon.current_version
        assert not version.needs_human_review
        assert addon.auto_approval_delayed_until is None
        _delay_auto_approval_indefinitely_and_restrict_future_approvals(version)
        assert addon.auto_approval_delayed_until == datetime.max
        assert version.needs_human_review
        # We added a new restriction for approval without touching the existing one
        # for submission for user1 and user3
        assert EmailUserRestriction.objects.filter(
            email_pattern=user1.email, restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user1.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user2.email, restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user3.email, restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert EmailUserRestriction.objects.filter(
            email_pattern=user3.email, restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert not EmailUserRestriction.objects.filter(
            email_pattern=user4.email
        ).exists()

        # Like above, we added a new restriction for approval, this time for the ip,
        # but we left the one for submission.
        assert IPNetworkUserRestriction.objects.filter(
            network='5.6.7.8/32', restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert IPNetworkUserRestriction.objects.filter(
            network='5.6.7.8/32', restriction_type=RESTRICTION_TYPES.SUBMISSION
        ).exists()
        assert IPNetworkUserRestriction.objects.filter(
            network='1.2.3.4/32', restriction_type=RESTRICTION_TYPES.APPROVAL
        ).exists()
        assert not IPNetworkUserRestriction.objects.filter(network=None).exists()
        assert not IPNetworkUserRestriction.objects.filter(network='').exists()

    def test_delay_auto_approval_indefinitely_and_restrict_nothing_to_restrict(self):
        addon = addon_factory()
        version = addon.current_version
        assert not version.needs_human_review
        assert addon.auto_approval_delayed_until is None
        _delay_auto_approval_indefinitely(version)
        assert addon.auto_approval_delayed_until == datetime.max
        assert version.needs_human_review

    def test_flag_for_human_review_by_scanner(self):
        version = version_factory(addon=addon_factory())
        with self.assertRaises(VersionReviewerFlags.DoesNotExist):
            version.reviewerflags

        _flag_for_human_review_by_scanner(version, MAD)

        assert version.reviewerflags.needs_human_review_by_mad

    def test_flag_for_human_review_by_scanner_with_existing_flags(self):
        version = version_factory(addon=addon_factory())
        version_review_flags_factory(version=version)

        assert not version.reviewerflags.needs_human_review_by_mad

        _flag_for_human_review_by_scanner(version, MAD)
        version.refresh_from_db()

        assert version.reviewerflags.needs_human_review_by_mad

    def test_flag_for_human_review_by_scanner_raises_if_not_mad(self):
        version = version_factory(addon=addon_factory())

        with self.assertRaises(ValueError):
            assert _flag_for_human_review_by_scanner(version, CUSTOMS)


class TestRunAction(TestCase):
    def setUp(self):
        super().setUp()

        self.scanner = YARA
        self.version = version_factory(addon=addon_factory())
        self.scanner_rule = ScannerRule.objects.create(
            name='rule-1', scanner=self.scanner, action=NO_ACTION
        )
        self.scanner_result = ScannerResult.objects.create(
            version=self.version, scanner=self.scanner
        )
        self.scanner_result.matched_rules.add(self.scanner_rule)

    @mock.patch('olympia.scanners.models._no_action')
    def test_runs_no_action(self, no_action_mock):
        self.scanner_rule.update(action=NO_ACTION)

        ScannerResult.run_action(self.version)

        assert no_action_mock.called
        no_action_mock.assert_called_with(self.version)

    @mock.patch('olympia.scanners.models._flag_for_human_review')
    def test_runs_flag_for_human_review(self, flag_for_human_review_mock):
        self.scanner_rule.update(action=FLAG_FOR_HUMAN_REVIEW)

        ScannerResult.run_action(self.version)

        assert flag_for_human_review_mock.called
        flag_for_human_review_mock.assert_called_with(self.version)

    @mock.patch('olympia.scanners.models._delay_auto_approval')
    def test_runs_delay_auto_approval(self, _delay_auto_approval_mock):
        self.scanner_rule.update(action=DELAY_AUTO_APPROVAL)

        ScannerResult.run_action(self.version)

        assert _delay_auto_approval_mock.called
        _delay_auto_approval_mock.assert_called_with(self.version)

    @mock.patch('olympia.scanners.models._delay_auto_approval_indefinitely')
    def test_runs_delay_auto_approval_indefinitely(
        self, _delay_auto_approval_indefinitely_mock
    ):
        self.scanner_rule.update(action=DELAY_AUTO_APPROVAL_INDEFINITELY)

        ScannerResult.run_action(self.version)

        assert _delay_auto_approval_indefinitely_mock.called
        _delay_auto_approval_indefinitely_mock.assert_called_with(self.version)

    @mock.patch('olympia.scanners.models._delay_auto_approval_indefinitely')
    def test_returns_for_non_extension_addons(
        self, _delay_auto_approval_indefinitely_mock
    ):
        self.scanner_rule.update(action=DELAY_AUTO_APPROVAL_INDEFINITELY)
        self.version.addon.update(type=amo.ADDON_DICT)

        ScannerResult.run_action(self.version)

        assert not _delay_auto_approval_indefinitely_mock.called

        self.version.addon.update(type=amo.ADDON_LPAPP)

        ScannerResult.run_action(self.version)

        assert not _delay_auto_approval_indefinitely_mock.called

        self.version.addon.update(type=amo.ADDON_STATICTHEME)

        ScannerResult.run_action(self.version)

        assert not _delay_auto_approval_indefinitely_mock.called

    @mock.patch('olympia.scanners.models.log.info')
    def test_returns_when_no_action_found(self, log_mock):
        self.scanner_rule.delete()

        ScannerResult.run_action(self.version)

        log_mock.assert_called_with(
            'No action to execute for version %s.', self.version.id
        )

    def test_raise_when_action_is_invalid(self):
        # `12345` is an invalid action ID
        self.scanner_rule.update(action=12345)

        with pytest.raises(Exception, match='invalid action 12345'):
            ScannerResult.run_action(self.version)

    @mock.patch('olympia.scanners.models._no_action')
    @mock.patch('olympia.scanners.models._flag_for_human_review')
    def test_selects_the_action_with_the_highest_severity(
        self, flag_for_human_review_mock, no_action_mock
    ):
        # Create another rule and add it to the current scanner result. This
        # rule is more severe than `rule-1` created in `setUp()`.
        rule = ScannerRule.objects.create(
            name='rule-2', scanner=self.scanner, action=FLAG_FOR_HUMAN_REVIEW
        )
        self.scanner_result.matched_rules.add(rule)

        ScannerResult.run_action(self.version)

        assert not no_action_mock.called
        assert flag_for_human_review_mock.called

    @mock.patch('olympia.scanners.models._no_action')
    @mock.patch('olympia.scanners.models._flag_for_human_review')
    def test_selects_active_actions_only(
        self, flag_for_human_review_mock, no_action_mock
    ):
        # Create another rule and add it to the current scanner result. This
        # rule is more severe than `rule-1` created in `setUp()`. In this test
        # case, we disable this rule, though.
        rule = ScannerRule.objects.create(
            name='rule-2',
            scanner=self.scanner,
            action=FLAG_FOR_HUMAN_REVIEW,
            is_active=False,
        )
        self.scanner_result.matched_rules.add(rule)

        ScannerResult.run_action(self.version)

        assert no_action_mock.called
        assert not flag_for_human_review_mock.called

    def test_flags_for_human_review_by_mad_when_score_is_too_low(self):
        version = version_factory(addon=addon_factory())
        results = {'scanners': {'customs': {'score': 0.001}}}
        ScannerResult.objects.create(version=version, scanner=MAD, results=results)

        ScannerResult.run_action(version)

        assert version.reviewerflags.needs_human_review_by_mad

    def test_flags_for_human_review_by_mad_when_score_is_too_high(self):
        version = version_factory(addon=addon_factory())
        results = {'scanners': {'customs': {'score': 0.99}}}
        ScannerResult.objects.create(version=version, scanner=MAD, results=results)

        ScannerResult.run_action(version)

        assert version.reviewerflags.needs_human_review_by_mad

    def test_flags_for_human_review_by_mad_when_models_disagree(self):
        version = version_factory(addon=addon_factory())
        results = {'scanners': {'customs': {'result_details': {'models_agree': False}}}}
        ScannerResult.objects.create(version=version, scanner=MAD, results=results)

        ScannerResult.run_action(version)

        assert version.reviewerflags.needs_human_review_by_mad

    def test_does_not_flag_for_human_review_by_mad_when_score_is_okay(self):
        version = version_factory(addon=addon_factory())
        results = {'scanners': {'customs': {'score': 0.2}}}
        ScannerResult.objects.create(version=version, scanner=MAD, results=results)

        ScannerResult.run_action(version)

        with self.assertRaises(VersionReviewerFlags.DoesNotExist):
            version.reviewerflags
