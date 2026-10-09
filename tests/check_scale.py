"""Explicit 100k-row verification; run separately from the quick integration suite.

TEST_MONGO_URI selects MongoDB. Creates and drops only a random attendance_scale_*
database. Test data is generated in memory; no data dump is written.
"""
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['MONGO_URI'] = os.getenv('TEST_MONGO_URI', 'mongodb://127.0.0.1:27017')
os.environ['MONGO_DB'] = 'attendance_scale_' + uuid.uuid4().hex
from fastapi.testclient import TestClient
from app import main as api


def rounded(value, digits=2):
    return float(Decimal(str(value)).quantize(Decimal(10) ** -digits, rounding=ROUND_HALF_UP))


def main():
    report = {'mongodb': api.client.server_info()['version'], 'logs': 100000, 'employees': 1050}
    employees = [dict(emp_code=f'EMP{i:04}', name=f'Employee {i}', email=f'e{i}@example.com',
        department=f'Department {i % 5}', shift_start='09:30', shift_end='18:30',
        joined_on='2026-07-15' if i % 7 == 0 else '2026-01-01', created_at=datetime(2026,1,1,tzinfo=timezone.utc))
        for i in range(1050)]
    all_logs = []
    base = date(2026,5,1)
    for i in range(1000):
        for j in range(100):
            status = ['PRESENT','WFH','ON_DUTY','LEAVE','ABSENT'][(i+j)%5]
            presence = status in api.PRESENCE
            all_logs.append(dict(emp_code=f'EMP{i:04}', date=(base+timedelta(days=j)).isoformat(), status=status,
                work_hours=(3.5 if (i+j)%3==0 else 8.25) if presence else None,
                half_day=(i+j)%3==0 if presence else False,
                late_minutes=((i+j)%4)*10 if presence else 0, overtime_minutes=30 if presence and j%2 else 0))
    api.db.employees.insert_many(employees)
    api.db.attendance_logs.insert_many(all_logs)
    started=time.perf_counter()
    with TestClient(api.app) as http:
        assert http.get('/health').status_code==200
        report['startup_seconds']=round(time.perf_counter()-started,3)
        assert report['startup_seconds']<20
        paths = {
            'attendance_list': ('/attendance',{'page':3,'page_size':20,'status':'WFH','date_from':'2026-07-01','date_to':'2026-07-31'}),
            'employee_monthly': ('/analytics/employees/EMP0001/monthly',{'month':'2026-07'}),
            'department_summary': ('/analytics/departments/summary',{'month':'2026-07'}),
            'late_leaderboard': ('/analytics/leaderboard/late',{'month':'2026-07','limit':10}),
            'department_trend': ('/analytics/departments/Department 0/trend',{'from':'2026-07-01','to':'2026-07-31'}),
        }
        outputs={}
        for key,(path,params) in paths.items():
            begin=time.perf_counter()
            response=http.get(path,params=params)
            assert response.status_code==200,response.text
            outputs[key]=response.json()
            duration=time.perf_counter()-begin
            explain_params=dict(params)
            if key=='employee_monthly': explain_params['emp_code']='EMP0001'
            if key=='department_trend': explain_params['department']='Department 0'
            response=http.get('/admin/explain/'+key,params=explain_params)
            assert response.status_code==200,response.text
            raw=json.dumps(response.json()['explain'])
            assert 'IXSCAN' in raw and 'COLLSCAN' not in raw,raw
            report[key]={'seconds':round(duration,3),'IXSCAN':True,'COLLSCAN':False}
        july=[r for r in all_logs if '2026-07-01'<=r['date']<='2026-07-31']
        emp_map={e['emp_code']:e for e in employees}
        summary=[]
        for department in sorted({e['department'] for e in employees}):
            emps=[e for e in employees if e['department']==department and e['joined_on']<='2026-07-31']
            codes={e['emp_code'] for e in emps}
            rows=[r for r in july if r['emp_code'] in codes]
            hours=[Decimal(str(r['work_hours'])) for r in rows if r['status'] in api.PRESENCE and r['work_hours'] is not None]
            summary.append(dict(department=department,headcount=len(emps),
                present_days=sum((0.5 if r['half_day'] else 1) for r in rows if r['status'] in api.PRESENCE and date.fromisoformat(r['date']).weekday()<5),
                avg_work_hours=rounded(sum(hours)/len(hours)) if hours else None,
                late_count=sum(r['late_minutes']>0 for r in rows),total_late_minutes=sum(r['late_minutes'] for r in rows),
                leave_count=sum(r['status']=='LEAVE' for r in rows),on_duty_count=sum(r['status']=='ON_DUTY' for r in rows)))
        assert outputs['department_summary']['items']==summary
        totals={}
        for r in july:
            values=totals.setdefault(r['emp_code'],[0,0])
            values[0]+=r['late_minutes']; values[1]+=r['late_minutes']>0
        ordered=sorted([(code,*values) for code,values in totals.items() if values[0]>0],key=lambda r:(-r[1],r[0]))
        leaderboard=[]
        previous=None
        for position,(code,total,count) in enumerate(ordered,1):
            if total!=previous: rank=position
            previous=total
            if rank<=10:
                e=emp_map[code]
                leaderboard.append(dict(emp_code=code,name=e['name'],department=e['department'],rank=rank,total_late_minutes=total,late_count=count))
        assert outputs['late_leaderboard']['items']==leaderboard
        trend=[]
        for offset in range(31):
            day=date(2026,7,1)+timedelta(days=offset)
            day_str=day.isoformat()
            codes={e['emp_code'] for e in employees if e['department']=='Department 0' and e['joined_on']<=day_str}
            rows=[r for r in july if r['date']==day_str and r['emp_code'] in codes]
            present=sum((0.5 if r['half_day'] else 1) for r in rows if r['status'] in api.PRESENCE)
            rate=rounded(Decimal(str(present))/len(codes),4) if day.weekday()<5 and codes else None
            rates=[r['attendance_rate'] for r in trend[-6:]]+[rate]
            rates=[Decimal(str(r)) for r in rates if r is not None]
            trend.append(dict(date=day_str,is_working_day=day.weekday()<5,headcount=len(codes),present_count=present,
                late_count=sum(r['late_minutes']>0 for r in rows),attendance_rate=rate,
                moving_avg_7d=rounded(sum(rates)/len(rates),4) if rates else None))
        assert outputs['department_trend']['items']==trend
        report['independent_oracle']='department summary, leaderboard and trend match'
        print(json.dumps(report,indent=2))
        Path('.local').mkdir(exist_ok=True)
        Path('.local/scale-results.json').write_text(json.dumps(report,indent=2))
        api.client.drop_database(api.db.name)


if __name__=='__main__':
    try:
        main()
    finally:
        # The client in the app lifespan may already be closed after a failure.
        from pymongo import MongoClient
        with MongoClient(os.environ['MONGO_URI']) as cleanup:
            cleanup.drop_database(os.environ['MONGO_DB'])
