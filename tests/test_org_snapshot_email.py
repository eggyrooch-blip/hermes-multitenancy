"""Sanitized shapes from the actual Contact response and org snapshot."""
import json

import pytest

from hermes_multitenancy import billing_identity as bi
from hermes_multitenancy.sync.feishu_org import Department, FeishuOrgSyncError, _department_user_from_api, build_org_snapshot


# Production sample contained exactly these four fields, with no address.
RAW_WITHOUT_EMAIL = {'user_id': 'employee-a', 'open_id': 'ou_employee_a', 'union_id': 'on_employee_a', 'mobile_visible': False}


def snapshot_for(raws):
    departments = [Department(dept_id=str(index), name='Department') for index in range(len(raws))]
    return build_org_snapshot(departments, {str(index): [_department_user_from_api(raw)] for index, raw in enumerate(raws)})


def strict_email(snapshot, tmp_path, monkeypatch):
    (tmp_path / 'org-1.json').write_text(json.dumps(snapshot.to_dict()))
    monkeypatch.setenv('HERMES_ORG_SNAPSHOT_DIR', str(tmp_path))
    return bi._employee_org_fields('employee-a', require_verified=True)[0]


@pytest.mark.parametrize('fields,expected', [
    ({'enterprise_email': 'canonical@example.com', 'email': 'secondary@example.com'}, 'canonical@example.com'),
    ({'email': 'canonical@example.com'}, 'canonical@example.com'),
])
def test_explicit_contact_addresses_survive_to_strict_billing(fields, expected, tmp_path, monkeypatch):
    snapshot = snapshot_for([{**RAW_WITHOUT_EMAIL, **fields}])
    employee = snapshot.to_dict()['employees']['employee-a']
    for field, value in fields.items():
        assert employee[field] == value
    assert strict_email(snapshot, tmp_path, monkeypatch) == expected


def test_real_missing_fields_stay_missing_and_fail_closed(tmp_path, monkeypatch):
    snapshot = snapshot_for([RAW_WITHOUT_EMAIL])
    employee = snapshot.to_dict()['employees']['employee-a']
    assert not employee.get('enterprise_email') and not employee.get('email')
    with pytest.raises(bi.RunRejected, match='org email missing'):
        strict_email(snapshot, tmp_path, monkeypatch)


@pytest.mark.parametrize('change', [{'open_id': 'ou_other'}, {'enterprise_email': 'other@example.com'}, {'email': 'other@example.com'}])
def test_conflicting_department_records_do_not_pick_first(change):
    raw = {**RAW_WITHOUT_EMAIL, 'enterprise_email': 'canonical@example.com', 'email': 'secondary@example.com'}
    with pytest.raises(FeishuOrgSyncError, match='identity conflict'):
        snapshot_for([raw, {**raw, **change}])


def test_duplicate_record_can_add_explicit_address_without_guessing(tmp_path, monkeypatch):
    snapshot = snapshot_for([RAW_WITHOUT_EMAIL, {**RAW_WITHOUT_EMAIL, 'enterprise_email': 'canonical@example.com'}])
    assert strict_email(snapshot, tmp_path, monkeypatch) == 'canonical@example.com'
    assert len(snapshot.employees) == 1
