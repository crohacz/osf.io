import functools
import mock
from django.db.models import Q
import pytest

from api.base.settings.defaults import API_BASE
from api_tests import utils as test_utils
from framework.auth.core import Auth
from osf.models import PreprintService, NodeLicense
from osf_tests.factories import (
    PreprintFactory, 
    AuthUserFactory, 
    ProjectFactory, 
    SubjectFactory, 
    PreprintProviderFactory,
)
from rest_framework import exceptions
from tests.base import fake, capture_signals
from website.project.signals import contributor_added
from website.identifiers.utils import build_ezid_metadata


def build_preprint_update_payload(node_id, attributes=None, relationships=None):
    payload = {
        'data': {
            'id': node_id,
            'attributes': attributes,
            'relationships': relationships
        }
    }
    return payload

@pytest.fixture()
def user():
    return AuthUserFactory()

@pytest.mark.django_db
class TestPreprintDetail:

    @pytest.fixture()
    def preprint(self, user):
        return PreprintFactory(creator=user)

    @pytest.fixture()
    def url(self, preprint):
        return '/{}preprints/{}/'.format(API_BASE, preprint._id)

    @pytest.fixture()
    def res(self, app, url):
        return app.get(url)

    @pytest.fixture()
    def data(self, res):
        return res.json['data']

    def test_preprint_detail(self, app, user, preprint, url, res, data):

    #   test_preprint_detail_success
        assert res.status_code == 200
        assert res.content_type == 'application/vnd.api+json'

    #   test_preprint_top_level
        assert data['type'] == 'preprints'
        assert data['id'] == preprint._id

    #   test title in preprint data
        assert data['attributes']['title'] == preprint.node.title

    #   test contributors in preprint data
        assert data['relationships'].get('contributors', None)

    #   test_preprint_node_deleted_detail_failure
        deleted_node = ProjectFactory(creator=user, is_deleted=True)
        deleted_preprint = PreprintFactory(project=deleted_node, creator=user)

        deleted_preprint_url = '/{}preprints/{}/'.format(API_BASE, deleted_preprint._id)
        deleted_preprint_res = app.get(deleted_preprint_url, expect_errors=True)
        assert deleted_preprint_res.status_code == 404
        assert res.content_type == 'application/vnd.api+json'

    def test_embed_contributors(self, app, user, preprint):
        url = '/{}preprints/{}/?embed=contributors'.format(API_BASE, preprint._id)

        res = app.get(url, auth=user.auth)
        embeds = res.json['data']['embeds']
        ids = preprint.node.contributors.all().values_list('guids___id', flat=True)
        ids = ['{}-{}'.format(preprint.node._id, id_) for id_ in ids]
        for contrib in embeds['contributors']['data']:
            assert contrib['id'] in ids


@pytest.mark.django_db
class TestPreprintDelete:

    @pytest.fixture()
    def unpublished_preprint(self, user):
        return PreprintFactory(creator=user, is_published=False)

    @pytest.fixture()
    def published_preprint(self, user):
        return PreprintFactory(creator=user)

    @pytest.fixture()
    def url(self, user):
        return '/{}preprints/{{}}/'.format(API_BASE)

    def test_can_delete_unpublished(self, app, user, url, unpublished_preprint):
        previous_ids = list(PreprintService.objects.all().values_list('pk', flat=True))
        app.delete(url.format(unpublished_preprint._id), auth=user.auth)
        remaining_ids = list(PreprintService.objects.all().values_list('pk', flat=True))
        assert unpublished_preprint.pk in previous_ids
        assert unpublished_preprint.pk not in remaining_ids

    def test_cannot_delete_published(self, app, user, published_preprint, url):
        previous_ids = list(PreprintService.objects.all().values_list('pk', flat=True))
        res = app.delete(url.format(published_preprint._id), auth=user.auth, expect_errors=True)
        remaining_ids = list(PreprintService.objects.all().values_list('pk', flat=True))
        assert res.status_code == 409
        assert previous_ids == remaining_ids
        assert published_preprint.pk in remaining_ids

    def test_deletes_only_requested_document(self, app, user, published_preprint, unpublished_preprint, url):
        previous_ids = list(PreprintService.objects.all().values_list('pk', flat=True))
        res = app.delete(url.format(unpublished_preprint._id), auth=user.auth)
        remaining_ids = list(PreprintService.objects.all().values_list('pk', flat=True))

        assert unpublished_preprint.pk in previous_ids
        assert published_preprint.pk in previous_ids

        assert unpublished_preprint.pk not in remaining_ids
        assert published_preprint.pk in remaining_ids

@pytest.mark.django_db
class TestPreprintUpdate:

    @pytest.fixture()
    def preprint(self, user):
        return PreprintFactory(creator=user)

    @pytest.fixture()
    def url(self, preprint):
        return '/{}preprints/{}/'.format(API_BASE, preprint._id)

    @pytest.fixture()
    def subject(self):
        return SubjectFactory()

    def test_update_preprint_permission_denied(self, app, preprint, url):
        update_doi_payload = build_preprint_update_payload(preprint._id, attributes={'article_doi': '10.123/456/789'})

        noncontrib = AuthUserFactory()

        res = app.patch_json_api(url, update_doi_payload, auth=noncontrib.auth, expect_errors=True)
        assert res.status_code == 403

        res = app.patch_json_api(url, update_doi_payload, expect_errors=True)
        assert res.status_code == 401

    def test_update_subjects(self, app, user, preprint, subject, url):
        assert not preprint.subjects.filter(_id=subject._id).exists()
        update_subjects_payload = build_preprint_update_payload(preprint._id, attributes={'subjects': [[subject._id]]})

        res = app.patch_json_api(url, update_subjects_payload, auth=user.auth)
        assert res.status_code == 200

        preprint.reload()
        assert preprint.subjects.filter(_id=subject._id).exists()

    def test_update_invalid_subjects(self, app, user, preprint, url):
        subjects = preprint.subjects
        update_subjects_payload = build_preprint_update_payload(preprint._id, attributes={'subjects': [['wwe']]})

        res = app.patch_json_api(url, update_subjects_payload, auth=user.auth, expect_errors=True)
        assert res.status_code == 400

        preprint.reload()
        assert preprint.subjects == subjects

    def test_update_primary_file(self, app, user, preprint, url):
        new_file = test_utils.create_test_file(preprint.node, user,  filename='shook_that_mans_hand.pdf')
        relationships = {
            'primary_file': {
                'data': {
                    'type': 'file',
                    'id': new_file._id
                }
            }
        }
        assert preprint.primary_file != new_file
        update_file_payload = build_preprint_update_payload(preprint._id, relationships=relationships)

        res = app.patch_json_api(url, update_file_payload, auth=user.auth)
        assert res.status_code == 200

        preprint.node.reload()
        assert preprint.primary_file == new_file

        log = preprint.node.logs.latest()
        assert log.action == 'preprint_file_updated'
        assert log.params.get('preprint') == preprint._id

    def test_new_primary_not_in_node(self, app, user, preprint, url):
        project = ProjectFactory()
        file_for_project = test_utils.create_test_file(project, user, filename='six_pack_novak.pdf')

        relationships = {
            'primary_file': {
                'data': {
                    'type': 'file',
                    'id': file_for_project._id
                }
            }
        }

        update_file_payload = build_preprint_update_payload(preprint._id, relationships=relationships)

        res = app.patch_json_api(url, update_file_payload, auth=user.auth, expect_errors=True)
        assert res.status_code == 400

        preprint.reload()
        assert preprint.primary_file != file_for_project

    def test_update_article_doi(self, app, user, preprint, url):
        new_doi = '10.1234/ASDFASDF'
        assert preprint.article_doi != new_doi
        update_subjects_payload = build_preprint_update_payload(preprint._id, attributes={'doi': new_doi})

        res = app.patch_json_api(url, update_subjects_payload, auth=user.auth)
        assert res.status_code == 200

        preprint.node.reload()
        assert preprint.article_doi == new_doi

        preprint_detail = app.get(url, auth=user.auth).json['data']
        assert preprint_detail['links']['doi'] == 'https://dx.doi.org/{}'.format(new_doi)

    def test_cannot_set_primary_file(self, app, user, preprint, url):

    #   test_write_contrib_cannot_set_primary_file
        read_write_contrib = AuthUserFactory()
        preprint.node.add_contributor(read_write_contrib, permissions=['read', 'write'], auth=Auth(user), save=True)
        new_file = test_utils.create_test_file(preprint.node, user, filename='lovechild_reason.pdf')

        data = {
            'data':{
                'type': 'primary_file',
                'id': preprint._id,
                'attributes': {},
                'relationships': {
                    'primary_file': {
                        'data': {
                            'type': 'file',
                            'id': new_file._id
                        }
                    }
                }
            }
        }

        res = app.patch_json_api(url, data, auth=read_write_contrib.auth, expect_errors=True)
        assert res.status_code == 403

    #   test_noncontrib_cannot_set_primary_file
        non_contrib = AuthUserFactory()
        new_file = test_utils.create_test_file(preprint.node, user, filename='flowerchild_nik.pdf')

        data = {
            'data':{
                'type': 'primary_file',
                'id': preprint._id,
                'attributes': {},
                'relationships': {
                    'primary_file': {
                        'data': {
                            'type': 'file',
                            'id': new_file._id
                        }
                    }
                }
            }
        }

        res = app.patch_json_api(url, data, auth=non_contrib.auth, expect_errors=True)
        assert res.status_code == 403

    def test_contribs_cannot_set_subjects(self, app, user, preprint, subject, url):

    # def test_write_contrib_cannot_set_subjects(self, app, user, preprint, subject, url):
        write_contrib = AuthUserFactory()
        preprint.node.add_contributor(write_contrib, permissions=['read', 'write'], auth=Auth(user), save=True)

        assert not preprint.subjects.filter(_id=subject._id).exists()
        update_subjects_payload = build_preprint_update_payload(preprint._id, attributes={'subjects': [[subject._id]]})

        res = app.patch_json_api(url, update_subjects_payload, auth=write_contrib.auth, expect_errors=True)
        assert res.status_code == 403

        assert not preprint.subjects.filter(_id=subject._id).exists()

    # def test_non_contrib_cannot_set_subjects(self, app, user, preprint, subject, url):
        non_contrib = AuthUserFactory()

        assert not preprint.subjects.filter(_id=subject._id).exists()

        update_subjects_payload = build_preprint_update_payload(preprint._id, attributes={'subjects': [[subject._id]]})

        res = app.patch_json_api(url, update_subjects_payload, auth=non_contrib.auth, expect_errors=True)
        assert res.status_code == 403

        assert not preprint.subjects.filter(_id=subject._id).exists()

    @mock.patch('website.preprints.tasks.get_and_set_preprint_identifiers.si')
    def test_update_published(self, mock_get_identifiers, app, user):
        unpublished = PreprintFactory(creator=user, is_published=False)
        url = '/{}preprints/{}/'.format(API_BASE, unpublished._id)
        payload = build_preprint_update_payload(unpublished._id, attributes={'is_published': True})
        res = app.patch_json_api(url, payload, auth=user.auth)
        unpublished.reload()
        assert unpublished.is_published

    @mock.patch('website.preprints.tasks.on_preprint_updated.s')
    def test_update_preprint_task_called_on_api_update(self, mock_on_preprint_updated, app, user, preprint, url):
        update_doi_payload = build_preprint_update_payload(preprint._id, attributes={'doi': '10.1234/ASDFASDF'})

        app.patch_json_api(url, update_doi_payload, auth=user.auth)
        preprint.node.reload()

        assert mock_on_preprint_updated.called

@pytest.mark.django_db
class TestPreprintUpdateLicense:

    @pytest.fixture()
    def admin_contrib(self):
        return AuthUserFactory()

    @pytest.fixture()
    def write_contrib(self):
        return AuthUserFactory()

    @pytest.fixture()
    def read_contrib(self):
        return AuthUserFactory()

    @pytest.fixture()
    def non_contrib(self):
        return AuthUserFactory()

    @pytest.fixture()
    def cc0_license(self):
        return NodeLicense.objects.filter(name='CC0 1.0 Universal').first()

    @pytest.fixture()
    def mit_license(self):
        return NodeLicense.objects.filter(name='MIT License').first()

    @pytest.fixture()
    def no_license(self):
        return NodeLicense.objects.filter(name='No license').first()

    @pytest.fixture()
    def preprint_provider(self, cc0_license, no_license):
        preprint_provider = PreprintProviderFactory()
        preprint_provider.licenses_acceptable = [cc0_license, no_license]
        preprint_provider.save()
        return preprint_provider

    @pytest.fixture()
    def preprint(self, admin_contrib, write_contrib, read_contrib, preprint_provider):
        preprint = PreprintFactory(creator=admin_contrib, provider=preprint_provider)
        preprint.node.add_contributor(write_contrib, auth=Auth(admin_contrib))
        preprint.node.add_contributor(read_contrib, auth=Auth(admin_contrib), permissions=['read'])
        preprint.node.save()
        return preprint

    @pytest.fixture()
    def url(self, preprint):
        return '/{}preprints/{}/'.format(API_BASE, preprint._id)

    @pytest.fixture()
    def make_payload(self):
        def payload(node_id, license_id=None, license_year=None, copyright_holders=None):
            attributes = {}

            if license_year and copyright_holders:
                attributes = {
                    'license_record': {
                        'year': license_year,
                        'copyright_holders': copyright_holders
                    }
                }
            elif license_year:
                attributes = {
                    'license_record': {
                        'year': license_year
                    }
                }
            elif copyright_holders:
                attributes = {
                    'license_record': {
                        'copyright_holders': copyright_holders
                    }
                }

            return {
                'data': {
                    'id': node_id,
                    'attributes': attributes,
                    'relationships': {
                        'license': {
                            'data': {
                                'type': 'licenses',
                                'id': license_id
                            }
                        }
                    }
                }
            } if license_id else {
                'data': {
                    'id': node_id,
                    'attributes': attributes
                }
            }

        return payload

    @pytest.fixture()
    def make_request(self, app):
        def request(url, data, auth=None, expect_errors=False):
            return app.patch_json_api(url, data, auth=auth, expect_errors=expect_errors)
        return request

    def test_admin_update_license_with_invalid_id(self, admin_contrib, preprint, url, make_payload, make_request):
        data = make_payload(
            node_id=preprint._id,
            license_id='thisisafakelicenseid'
        )

        assert preprint.license == None

        res = make_request(url, data, auth=admin_contrib.auth, expect_errors=True)
        assert res.status_code == 404
        assert res.json['errors'][0]['detail'] == 'Unable to find specified license.'

        preprint.reload()
        assert preprint.license == None

    def test_admin_can_update_license(self, admin_contrib, preprint, cc0_license, url, make_payload, make_request):
        data = make_payload(
            node_id=preprint._id,
            license_id=cc0_license._id
        )

        assert preprint.license == None

        res = make_request(url, data, auth=admin_contrib.auth)
        assert res.status_code == 200
        preprint.reload()

        assert preprint.license.node_license == cc0_license
        assert preprint.license.year == None
        assert preprint.license.copyright_holders == []

        # check logs
        log = preprint.node.logs.latest()
        assert log.action == 'preprint_license_updated'
        assert log.params.get('preprint') == preprint._id

    def test_admin_can_update_license_record(self, admin_contrib, preprint, no_license, url, make_payload, make_request):
        data = make_payload(
            node_id=preprint._id,
            license_id=no_license._id,
            license_year='2015',
            copyright_holders=['Tonya Shepoly, Lucas Pucas']
        )

        assert preprint.license == None

        res = make_request(url, data, auth=admin_contrib.auth)
        assert res.status_code == 200
        preprint.reload()

        assert preprint.license.node_license == no_license
        assert preprint.license.year == '2015'
        assert preprint.license.copyright_holders == ['Tonya Shepoly, Lucas Pucas']

    def test_cannot_update_license(self, write_contrib, read_contrib, non_contrib, preprint, cc0_license, url, make_payload, make_request):

    #   test_write_contrib_cannot_update_license
        data = make_payload(
            node_id=preprint._id,
            license_id=cc0_license._id
        )

        res = make_request(url, data, auth=write_contrib.auth, expect_errors=True)
        assert res.status_code == 403
        assert res.json['errors'][0]['detail'] == 'User must be an admin to update a preprint.'

    #   test_read_contrib_cannot_update_license
        data = make_payload(
            node_id=preprint._id,
            license_id=cc0_license._id
        )

        res = make_request(url, data, auth=read_contrib.auth, expect_errors=True)
        assert res.status_code == 403
        assert res.json['errors'][0]['detail'] == exceptions.PermissionDenied.default_detail

    #   test_non_contrib_cannot_update_license
        data = make_payload(
            node_id=preprint._id,
            license_id=cc0_license._id
        )

        res = make_request(url, data, auth=non_contrib.auth, expect_errors=True)
        assert res.status_code == 403
        assert res.json['errors'][0]['detail'] == exceptions.PermissionDenied.default_detail

    #   test_unauthenticated_user_cannot_update_license
        data = make_payload(
            node_id=preprint._id,
            license_id=cc0_license._id
        )

        res = make_request(url, data, expect_errors=True)
        assert res.status_code == 401
        assert res.json['errors'][0]['detail'] == exceptions.NotAuthenticated.default_detail

    def test_update_error(self, admin_contrib, preprint, preprint_provider, mit_license, no_license, url, make_payload, make_request):

    #   test_update_preprint_with_invalid_license_for_provider
        data = make_payload(
            node_id=preprint._id,
            license_id=mit_license._id
        )

        assert preprint.license == None

        res = make_request(url, data, auth=admin_contrib.auth, expect_errors=True)
        assert res.status_code == 403
        assert res.json['errors'][0]['detail'] == 'Invalid license chosen for {}'.format(preprint_provider.name)

    #   test_update_preprint_license_without_required_year_in_payload
        data = make_payload(
            node_id=preprint._id,
            license_id=no_license._id,
            copyright_holders=['Rachel', 'Rheisen']
        )

        res = make_request(url, data, auth=admin_contrib.auth, expect_errors=True)
        assert res.status_code == 400
        assert res.json['errors'][0]['detail'] == 'year must be specified for this license'

    #   test_update_preprint_license_without_required_copyright_holders_in_payload
        data = make_payload(
            node_id=preprint._id,
            license_id=no_license._id,
            license_year='1994'
        )

        res = make_request(url, data, auth=admin_contrib.auth, expect_errors=True)
        assert res.status_code == 400
        assert res.json['errors'][0]['detail'] == 'copyrightHolders must be specified for this license'

    def test_update_preprint_with_existing_license_year_attribute_only(self, admin_contrib, preprint, no_license, url, make_payload, make_request):
        preprint.set_preprint_license(
            {
                'id': no_license.license_id,
                'year': '2014',
                'copyrightHolders': ['Daniel FromBrazil', 'Queen Jaedyn']
            },
            Auth(admin_contrib),
        )
        preprint.save()

        assert preprint.license.node_license == no_license
        assert preprint.license.year == '2014'
        assert preprint.license.copyright_holders == ['Daniel FromBrazil', 'Queen Jaedyn']

        data = make_payload(
            node_id=preprint._id,
            license_year='2015'
        )

        res = make_request(url, data, auth=admin_contrib.auth)
        assert res.status_code == 200
        preprint.license.reload()

        assert preprint.license.node_license == no_license
        assert preprint.license.year == '2015'
        assert preprint.license.copyright_holders == ['Daniel FromBrazil', 'Queen Jaedyn']

    def test_update_preprint_with_existing_license_copyright_holders_attribute_only(self, admin_contrib, preprint, no_license, url, make_payload, make_request):
        preprint.set_preprint_license(
            {
                'id': no_license.license_id,
                'year': '2014',
                'copyrightHolders': ['Captain Haley', 'Keegor Cannoli']
            },
            Auth(admin_contrib),
        )
        preprint.save()

        assert preprint.license.node_license == no_license
        assert preprint.license.year == '2014'
        assert preprint.license.copyright_holders == ['Captain Haley', 'Keegor Cannoli']

        data = make_payload(
            node_id=preprint._id,
            copyright_holders=['Reason Danish', 'Ben the NJB']
        )

        res = make_request(url, data, auth=admin_contrib.auth)
        assert res.status_code == 200
        preprint.license.reload()

        assert preprint.license.node_license == no_license
        assert preprint.license.year == '2014'
        assert preprint.license.copyright_holders == ['Reason Danish', 'Ben the NJB']

    def test_update_preprint_with_existing_license_relationship_only(self, admin_contrib, preprint, cc0_license, no_license, url, make_payload, make_request):
        preprint.set_preprint_license(
            {
                'id': no_license.license_id,
                'year': '2014',
                'copyrightHolders': ['Reason', 'Mr. Lulu']
            },
            Auth(admin_contrib),
        )
        preprint.save()

        assert preprint.license.node_license == no_license
        assert preprint.license.year == '2014'
        assert preprint.license.copyright_holders == ['Reason', 'Mr. Lulu']

        data = make_payload(
            node_id=preprint._id,
            license_id=cc0_license._id
        )

        res = make_request(url, data, auth=admin_contrib.auth)
        assert res.status_code == 200
        preprint.license.reload()

        assert preprint.license.node_license == cc0_license
        assert preprint.license.year == '2014'
        assert preprint.license.copyright_holders == ['Reason', 'Mr. Lulu']

    def test_update_preprint_with_existing_license_relationship_and_attributes(self, admin_contrib, preprint, cc0_license, no_license, url, make_payload, make_request):
        preprint.set_preprint_license(
            {
                'id': no_license.license_id,
                'year': '2014',
                'copyrightHolders': ['Reason', 'Mr. Cosgrove']
            },
            Auth(admin_contrib),
            save=True
        )

        assert preprint.license.node_license == no_license
        assert preprint.license.year == '2014'
        assert preprint.license.copyright_holders == ['Reason', 'Mr. Cosgrove']

        data = make_payload(
            node_id=preprint._id,
            license_id=cc0_license._id,
            license_year='2015',
            copyright_holders=['Rheisen', 'Princess Tyler']
        )

        res = make_request(url, data, auth=admin_contrib.auth)
        assert res.status_code == 200
        preprint.license.reload()

        assert preprint.license.node_license == cc0_license
        assert preprint.license.year == '2015'
        assert preprint.license.copyright_holders == ['Rheisen', 'Princess Tyler']

    def test_update_preprint_license_does_not_change_project_license(self, admin_contrib, preprint, cc0_license, no_license, url, make_payload, make_request):
        preprint.node.set_node_license(
            {
                'id': no_license.license_id,
                'year': '2015',
                'copyrightHolders': ['Simba', 'Mufasa']
            },
            auth=Auth(admin_contrib)
        )
        preprint.node.save()
        assert preprint.node.node_license.node_license == no_license

        data = make_payload(
            node_id=preprint._id,
            license_id=cc0_license._id
        )

        res = make_request(url, data, auth=admin_contrib.auth)
        assert res.status_code == 200
        preprint.reload()

        assert preprint.license.node_license == cc0_license
        assert preprint.node.node_license.node_license == no_license

    def test_update_preprint_license_without_change_does_not_add_log(self, admin_contrib, preprint, no_license, url, make_payload, make_request):
        preprint.set_preprint_license(
            {
                'id': no_license.license_id,
                'year': '2015',
                'copyrightHolders': ['Kim', 'Kanye']
            },
            auth=Auth(admin_contrib),
            save=True
        )

        before_num_logs = preprint.node.logs.count()
        before_update_log = preprint.node.logs.latest()

        data = make_payload(
            node_id=preprint._id,
            license_id=no_license._id,
            license_year='2015',
            copyright_holders=['Kanye', 'Kim']
        )
        res = make_request(url, data, auth=admin_contrib.auth)
        preprint.node.reload()

        after_num_logs = preprint.node.logs.count()
        after_update_log = preprint.node.logs.latest()

        assert res.status_code == 200
        assert before_num_logs == after_num_logs
        assert before_update_log._id == after_update_log._id


@pytest.mark.django_db
class TestPreprintIsPublishedDetail:

    @pytest.fixture()
    def admin(self):
        return AuthUserFactory()

    @pytest.fixture()
    def write_contrib(self):
        return AuthUserFactory()

    @pytest.fixture()
    def non_contrib(self):
        return AuthUserFactory()

    @pytest.fixture()
    def public_project(self, admin, write_contrib):
        public_project = ProjectFactory(creator=admin, is_public=True)
        public_project.add_contributor(write_contrib, permissions=['read', 'write'], save=True)
        return public_project

    @pytest.fixture()
    def subject(self):
        return SubjectFactory()

    @pytest.fixture()
    def provider(self):
        return PreprintProviderFactory()

    @pytest.fixture()
    def file_one_public_project(self, admin, public_project):
        return test_utils.create_test_file(public_project, admin, 'toe_socks_and_sunrises.pdf')

    @pytest.fixture()
    def unpublished_preprint(self, admin, provider, subject, public_project):
        return PreprintFactory(creator=admin, filename='toe_socks_and_sunrises.pdf', provider=provider, subjects=[[subject._id]], project=public_project, is_published=False)

    @pytest.fixture()
    def url(self, unpublished_preprint):
        return '/{}preprints/{}/'.format(API_BASE, unpublished_preprint._id)

    def test_preprint_is_published_detail(self, app, admin, write_contrib, non_contrib, unpublished_preprint, url):

    #   test_unpublished_visible_to_admins
        res = app.get(url, auth=admin.auth)
        assert res.json['data']['id'] == unpublished_preprint._id

    #   test_unpublished_invisible_to_write_contribs
        res = app.get(url, auth=write_contrib.auth, expect_errors=True)
        assert res.status_code == 403

    #   test_unpublished_invisible_to_non_contribs
        res = app.get(url, auth=non_contrib.auth, expect_errors=True)
        assert res.status_code == 403

    #   test_unpublished_invisible_to_public
        res = app.get(url, expect_errors=True)
        assert res.status_code == 401
