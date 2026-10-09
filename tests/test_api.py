"""Integration tests against real MongoDB; each run owns a random test database."""
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

os.environ['MONGO_URI'] = os.getenv('TEST_MONGO_URI', 'mongodb://127.0.0.1:27017')
os.environ['MONGO_DB'] = 'attendance_test_' + uuid.uuid4().hex
from app import main as api


@pytest.fixture(scope='session')
def http():
    with TestClient(api.app) as client:
        assert client.get('/health').status_code == 200
        yield client
        api.client.drop_database(api.db.name)


@pytest.fixture(autouse=True)
def clean(http):
    api.db.attendance_logs.delete_many({})
    api.db.employees.delete_many({})


def ms(local):
    return int(datetime.fromisoformat(local).replace(tzinfo=api.IST).timestamp() * 1000)


def add_employee(http, code='EMP0001', **extra):
    data = dict(emp_code=code, name=code, email=code+'@test.example', department='Engineering', joined_on='2026-01-01')
    data.update(extra)
    response = http.post('/employees', json=data)
    assert response.status_code == 201, response.text
    return response.json()


def seed_log(code, day, status='PRESENT', **values):
    doc = dict(emp_code=code, date=day, status=status, punch_in=None, punch_out=None,
               work_hours=None, late_minutes=0, overtime_minutes=0)
    doc.update(values)
    api.db.attendance_logs.insert_one(doc)


def get(http, path, **params):
    response = http.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_health_failure(http, monkeypatch):
    from pymongo.errors import ServerSelectionTimeoutError
    def unavailable(*args, **kwargs):
        raise ServerSelectionTimeoutError('test outage')
    monkeypatch.setattr(type(api.client.admin), 'command', unavailable)
    assert http.get('/health').status_code == 503


@pytest.mark.parametrize('bad', [1783312500, 1783312500000.0, '1783312500000', True, None, 4102444800001, 99999999999])
def test_strict_timestamps(http, bad):
    add_employee(http)
    assert http.post('/attendance/punch-in', json={'emp_code':'EMP0001','punched_at':bad}).status_code == 422


@pytest.mark.parametrize('data', [dict(emp_code='bad'), dict(name=''), dict(email='a@b'), dict(department=''),
    dict(shift_start='9:30'), dict(shift_end='09:30'), dict(joined_on='2026-02-30'), dict(joined_on='2026-1-01')])
def test_employee_validation(http, data):
    body = dict(emp_code='EMP0001', name='One', email='one@example.com', department='Dev', joined_on='2026-01-01') | data
    assert http.post('/employees', json=body).status_code == 422


@pytest.mark.parametrize('time,expected', [('09:40:00.900',0), ('09:40:01',10), ('10:15:59',45)])
def test_late_grace_and_truncation(http,time,expected):
    add_employee(http)
    r=http.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':ms('2026-07-06T'+time),'late_minutes':999})
    assert r.status_code == 201, r.text
    assert r.json()['late_minutes'] == expected
    assert r.json()['punch_in'] % 1000 == 0
    assert 'id' not in r.json() and '_id' not in r.json()
    stored=api.db.attendance_logs.find_one({})
    assert isinstance(stored['punch_in'],datetime)


def test_overnight_and_round_half_up(http):
    add_employee(http,shift_start='22:00',shift_end='06:00')
    pi=ms('2026-07-07T01:00:00')
    r=http.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':pi})
    assert r.json()['date']=='2026-07-06'
    assert r.json()['late_minutes']==180
    r=http.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':ms('2026-07-07T06:40:00')})
    assert r.status_code==200,r.text
    assert r.json()['overtime_minutes']==40
    assert r.json()['work_hours']==5.67
    assert r.json()['history']==[]
    assert api.compute_work_hours(api.instant(pi),api.instant(pi)+timedelta(seconds=450))==0.13


@pytest.mark.parametrize('seconds,half_day,hours',[(16181,True,4.49),(16182,False,4.5)])
def test_half_day_rounded_threshold(http,seconds,half_day,hours):
    add_employee(http)
    pi=ms('2026-07-06T09:30:00')
    http.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':pi})
    r=http.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':pi+seconds*1000})
    assert r.status_code==200,r.text
    assert r.json()['half_day'] is half_day
    assert r.json()['work_hours']==hours


@pytest.mark.parametrize('out,expected',[('18:59:59',0),('19:00:00',30)])
def test_overtime_minimum(http,out,expected):
    add_employee(http)
    http.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':ms('2026-07-06T09:30:00')})
    r=http.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':ms('2026-07-06T'+out)})
    assert r.json()['overtime_minutes']==expected


def test_punch_errors(http):
    assert http.post('/attendance/punch-in',json={'emp_code':'missing'}).status_code==404
    add_employee(http)
    pi=ms('2026-07-06T09:30:00')
    assert http.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':pi}).status_code==404
    http.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':pi})
    for out in [pi,pi+86401000]:
        assert http.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':out}).status_code==422
    assert http.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':pi+86400000}).status_code==200
    assert http.post('/attendance/punch-out',json={'emp_code':'EMP0001','punched_at':pi+86400000}).status_code==409


def test_corrections_and_legacy_history(http):
    add_employee(http)
    pi=ms('2026-07-06T10:05:00')
    seed_log('EMP0001','2026-07-06',punch_in=api.instant(pi),punch_out=api.instant(pi+5*3600000),work_hours=5,late_minutes=35)
    url='/attendance/EMP0001/2026-07-06'
    body={'reason':'Biometric correction','regularized_by':'HR','punch_in':ms('2026-07-06T09:28:00')}
    r=http.patch(url,json=body)
    assert r.status_code==200,r.text
    row=r.json()
    assert row['late_minutes']==0 and row['work_hours']==5.62
    assert row['history'][0]['changes']=={'punch_in':{'from':pi,'to':body['punch_in']},
        'work_hours':{'from':5,'to':5.62},'late_minutes':{'from':35,'to':0}}
    assert isinstance(row['history'][0]['at'],int)
    assert http.patch(url,json=body).status_code==422
    assert http.patch(url,json=body|{'punch_in':ms('2026-07-07T09:28:00')}).status_code==422
    assert http.patch(url,json=body|{'status':'LEAVE'}).status_code==422
    r=http.patch(url,json={'reason':'Approved leave','regularized_by':'HR','status':'LEAVE'})
    assert r.status_code==200
    row=r.json()
    assert row['punch_in'] is None and row['punch_out'] is None and row['work_hours'] is None
    assert len(row['history'])==2
    assert http.patch(url,json={'reason':'Return to work','regularized_by':'HR','status':'WFH'}).status_code==422
    assert http.patch(url,json=body|{'status':'WFH'}).status_code==200
    stored=api.db.attendance_logs.find_one({})
    assert isinstance(stored['history'][0]['changes']['punch_in']['from'],datetime)


def parallel(fn,n=8):
    barrier=threading.Barrier(n)
    def run(i):
        barrier.wait()
        return fn(i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run,range(n)))


def test_parallel_employee_and_punches(http):
    body=dict(emp_code='EMP0001',name='One',email='one@example.com',department='Dev',joined_on='2026-01-01')
    statuses=parallel(lambda _:http.post('/employees',json=body).status_code)
    assert sorted(statuses)==[201]+[409]*7
    body={'emp_code':'EMP0001','punched_at':ms('2026-07-06T09:30:00')}
    statuses=parallel(lambda _:http.post('/attendance/punch-in',json=body).status_code)
    assert sorted(statuses)==[201]+[409]*7
    body['punched_at']=ms('2026-07-06T18:30:00')
    statuses=parallel(lambda _:http.post('/attendance/punch-out',json=body).status_code)
    assert sorted(statuses)==[200]+[409]*7


def test_parallel_corrections_preserve_history(http):
    add_employee(http)
    http.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':ms('2026-07-06T09:30:00')})
    statuses=parallel(lambda i:http.patch('/attendance/EMP0001/2026-07-06',json={
        'punch_in':ms('2026-07-06T10:00:00')+i*60000,'reason':f'Correction {i}','regularized_by':'HR'}).status_code)
    assert all(s in [200,409] for s in statuses)
    row=get(http,'/attendance')['items'][0]
    assert len(row['history'])==statuses.count(200)
    before=ms('2026-07-06T09:30:00')
    for entry in row['history']:
        assert entry['changes']['punch_in']['from']==before
        before=entry['changes']['punch_in']['to']
    assert row['punch_in']==before


def test_pagination_filters_and_order(http):
    for i in [3,1,2]:
        add_employee(http,f'EMP{i:04}',department='Dev' if i<3 else 'Ops')
        for day in ['2026-07-06','2026-07-07']:
            seed_log(f'EMP{i:04}',day,status='WFH' if i<3 else 'LEAVE')
    rows=get(http,'/employees',department='Dev',page_size=1)
    assert rows['total']==2 and rows['items'][0]['emp_code']=='EMP0001'
    rows=get(http,'/attendance',status='WFH',page_size=2)
    assert rows['total']==4
    assert [(r['date'],r['emp_code']) for r in rows['items']]==[('2026-07-07','EMP0001'),('2026-07-07','EMP0002')]
    assert rows['items'][0]['history']==[] and rows['items'][0]['half_day'] is False
    assert get(http,'/attendance',emp_code='EMP0001',date_from='2026-07-07')['total']==1
    for params in [dict(page=0),dict(page_size=101),dict(status='BAD'),dict(date_from='2026-02-30'),
                   dict(date_from='2026-07-07',date_to='2026-07-06')]:
        assert http.get('/attendance',params=params).status_code==422


def test_analytics_adversarial_data(http):
    add_employee(http,'EMP0001')
    add_employee(http,'EMP0002',joined_on='2026-07-15')
    add_employee(http,'EMP0003') # zero logs
    add_employee(http,'EMP0004',joined_on='2026-08-01',department='Future')
    add_employee(http,'EMP0005',department='Sales')
    # Deliberately store derived values inconsistent with times: analytics must use storage.
    seed_log('EMP0001','2026-07-06',work_hours=8,late_minutes=40)
    seed_log('EMP0001','2026-07-07',status='WFH',work_hours=3.5,half_day=True)
    seed_log('EMP0001','2026-07-11',status='ON_DUTY',work_hours=7,late_minutes=20,overtime_minutes=35)
    seed_log('EMP0001','2026-07-08',status='LEAVE')
    seed_log('EMP0002','2026-07-15',status='ON_DUTY',work_hours=9,late_minutes=60)
    seed_log('EMP0005','2026-07-06',work_hours=8,late_minutes=100)
    seed_log('ORPHAN','2026-07-06',late_minutes=1000)
    monthly=get(http,'/analytics/employees/EMP0001/monthly',month='2026-07')
    assert monthly==dict(emp_code='EMP0001',month='2026-07',working_days=23,present_days=1.5,leave_days=1,
        late_count=2,total_late_minutes=60,total_overtime_minutes=35,attendance_pct=6.52)
    assert get(http,'/analytics/employees/EMP0002/monthly',month='2026-07')['working_days']==13
    future=get(http,'/analytics/employees/EMP0004/monthly',month='2026-07')
    assert future['working_days']==0 and future['attendance_pct'] is None
    summary=get(http,'/analytics/departments/summary',month='2026-07')['items']
    assert [s['department'] for s in summary]==['Engineering','Sales']
    assert summary[0]==dict(department='Engineering',headcount=3,present_days=2.5,avg_work_hours=6.88,
        late_count=3,total_late_minutes=120,leave_count=1,on_duty_count=2)
    board=get(http,'/analytics/leaderboard/late',month='2026-07',limit=2)['items']
    assert [(r['emp_code'],r['rank']) for r in board]==[('EMP0005',1),('EMP0001',2),('EMP0002',2)]
    board=get(http,'/analytics/leaderboard/late',month='2026-07',limit=1,department='Engineering')['items']
    assert [(r['emp_code'],r['rank']) for r in board]==[('EMP0001',1),('EMP0002',1)]
    trend=get(http,'/analytics/departments/Engineering/trend',**{'from':'2026-07-06','to':'2026-07-15'})['items']
    assert len(trend)==10
    assert trend[0]['headcount']==2 and trend[-1]['headcount']==3
    assert trend[0]['attendance_rate']==0.5 and trend[1]['attendance_rate']==0.25
    assert trend[1]['moving_avg_7d']==0.375
    assert trend[2]['attendance_rate']==0 and trend[2]['moving_avg_7d']==0.25
    assert trend[5]['present_count']==1 and trend[5]['attendance_rate'] is None
    assert trend[7]['moving_avg_7d']==0.05
    assert trend[-1]['attendance_rate']==0.3333


def test_empty_trend_zero_headcount_and_rounding(http):
    add_employee(http,joined_on='2026-07-06')
    rows=get(http,'/analytics/departments/Engineering/trend',**{'from':'2026-07-04','to':'2026-07-07'})['items']
    assert [r['headcount'] for r in rows]==[0,0,1,1]
    assert [r['attendance_rate'] for r in rows]==[None,None,0,0]
    assert [r['moving_avg_7d'] for r in rows]==[None,None,0,0]
    empty=get(http,'/analytics/departments/summary',month='2026-07')['items'][0]
    assert empty['headcount']==1 and empty['avg_work_hours'] is None
    seed_log('EMP0001','2026-07-06',work_hours=1.0)
    seed_log('EMP0001','2026-07-07',work_hours=1.01)
    assert get(http,'/analytics/departments/summary',month='2026-07')['items'][0]['avg_work_hours']==1.01


@pytest.mark.parametrize('path,params,code',[
    ('/analytics/employees/unknown/monthly',{'month':'2026-07'},404),
    ('/analytics/departments/summary',{'month':'2026-13'},422),
    ('/analytics/departments/summary',{'month':'0000-01'},422),
    ('/analytics/leaderboard/late',{'month':'2026-07','limit':51},422),
    ('/analytics/departments/Missing/trend',{'from':'2026-07-01','to':'2026-07-02'},404),
    ('/analytics/departments/Missing/trend',{'from':'2026-07-02','to':'2026-07-01'},422),
    ('/analytics/departments/Missing/trend',{'from':'2026-01-01','to':'2026-04-03'},422),
    ('/admin/explain/department_trend',{'department':'Dev','from':'2026-07-01'},422),
    ('/admin/explain/employee_monthly',{'month':'2026-07'},422),
    ('/admin/explain/unknown',{},422),
])
def test_analytics_validation(http,path,params,code):
    assert http.get(path,params=params).status_code==code


@pytest.mark.parametrize('endpoint,params',[
    ('attendance_list',{'page':2,'page_size':1}),
    ('attendance_list',{'emp_code':'EMP0001','date_from':'2026-07-01','status':'WFH'}),
    ('employee_monthly',{'emp_code':'EMP0001','month':'2026-07'}),
    ('department_summary',{'month':'2026-07'}),
    ('late_leaderboard',{'month':'2026-07','limit':2}),
    ('department_trend',{'department':'Engineering','from':'2026-07-01','to':'2026-07-10'}),
])
def test_explain_uses_indexes(http,endpoint,params):
    add_employee(http)
    seed_log('EMP0001','2026-07-06',late_minutes=10)
    output=get(http,'/admin/explain/'+endpoint,**params)
    raw=json.dumps(output['explain'])
    assert 'IXSCAN' in raw,raw
    assert 'COLLSCAN' not in raw,raw
    assert 'executionStats' in raw or 'executionTimeMillisEstimate' in raw


def test_attendance_date_boundaries(http):
    add_employee(http,shift_start='22:00',shift_end='06:00')
    rows=[]
    for code,time in [('EMP0001','2026-07-07T05:59:59'),('EMP0002','2026-07-07T06:00:00')]:
        if code=='EMP0002': add_employee(http,code,shift_start='22:00',shift_end='06:00')
        response=http.post('/attendance/punch-in',json={'emp_code':code,'punched_at':ms(time)})
        assert response.status_code==201,response.text
        rows.append(response.json()['date'])
    assert rows==['2026-07-06','2026-07-07']
    add_employee(http,'EMP0003',shift_start='01:00',shift_end='09:00')
    response=http.post('/attendance/punch-in',json={'emp_code':'EMP0003','punched_at':ms('2026-07-07T01:00:00')})
    assert response.json()['date']=='2026-07-07' # UTC is still July 6.


def test_closed_latest_record_does_not_close_an_older_open_record(http):
    add_employee(http)
    for day in ['2026-07-06','2026-07-07']:
        assert http.post('/attendance/punch-in',json={'emp_code':'EMP0001','punched_at':ms(day+'T09:30:00')}).status_code==201
    out={'emp_code':'EMP0001','punched_at':ms('2026-07-07T18:30:00')}
    assert http.post('/attendance/punch-out',json=out).status_code==200
    assert http.post('/attendance/punch-out',json=out).status_code==409
    older=get(http,'/attendance',date_to='2026-07-06')['items'][0]
    assert older['punch_out'] is None


def test_moving_average_half_up_and_92_day_range(http):
    for i in range(1,6): add_employee(http,f'EMP{i:04}')
    # Rates 1/4 and 1/5 = .25 and .2 do not exercise 4dp ties;
    # instead seed 32 employees and one presence: rate .03125 -> .0313.
    for i in range(6,33): add_employee(http,f'EMP{i:04}')
    seed_log('EMP0001','2026-07-06')
    rows=get(http,'/analytics/departments/Engineering/trend',**{'from':'2026-07-06','to':'2026-07-07'})['items']
    assert rows[0]['attendance_rate']==0.0313
    assert rows[1]['moving_avg_7d']==0.0157
    rows=get(http,'/analytics/departments/Engineering/trend',**{'from':'2026-07-01','to':'2026-09-30'})['items']
    assert len(rows)==92
