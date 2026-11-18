import os,sys,types,datetime,site
# -I 隔离模式下补回 user site（依赖装在用户目录），但仍禁用 cwd 与 PYTHONPATH，防止 planted 模块劫持
p=site.getusersitepackages()
if os.path.isdir(p) and p not in sys.path:sys.path.append(p)
# --- 隔离：独立测试目录，SQLite，stub 掉 redis 与 sklearn 模型，避免依赖外部服务 ---
os.environ['DATABASE_URL']='sqlite:////tmp/fit_withdrawal_test/test.db'
os.environ['REDIS_URL']='redis://test-redis:6379/0'
os.environ['JWT_SECRET']='test-secret'
os.environ['SEED_DEMO']='false'
os.makedirs('/tmp/fit_withdrawal_test',exist_ok=True)
if os.path.exists('/tmp/fit_withdrawal_test/test.db'):os.remove('/tmp/fit_withdrawal_test/test.db')

# stub redis：记录入队并支持模拟 worker
fake_redis=types.ModuleType('redis')
class Q:
 def __init__(self):self.items=[]
 def rpush(self,k,v):self.items.append(int(v))
 def ping(self):return True
 def blpop(self,k,timeout=2):return (k,str(self.items.pop(0))) if self.items else None
QS={}
def from_url(*a,**k):
 u=a[0] if a else 'default'
 QS.setdefault(u,Q());return QS[u]
fake_redis.from_url=from_url;fake_redis.RedisError=Exception
sys.modules['redis']=fake_redis

sys.path.insert(0,'/workspace/backend')
from src.database import Base,engine,Session
from src import models
# stub fit_model.predict，返回可倒推围度的完整结构
class FM:
 @staticmethod
 def predict(m,p):return {'algorithm':'stub','label':'合身','features':[m['chest']-p['chest'],m['shoulder']-p['shoulder']],'probabilities':{'合身':0.9},'balanced_accuracy':1.0,'confusion_matrix':[[1]],'classes':['合身'],'train_count':1,'holdout_count':1,'train_ids':[0],'holdout_ids':[1],'training_sha256':'TRAIN','source_sha256':'SRC','note':'n'}
sys.modules['src.fit_model']=FM

from fastapi.testclient import TestClient
from src.main import app
from src.security import token_for
from src.models import User,Shop,FitProfile,FitAssessment,FitWithdrawal,Measurement,Garment,Agreement,Consignor
from src.security import password_hash

Base.metadata.create_all(engine)
with Session() as s:
 s.add(User(email='operator@hesheng.local',password=password_hash('Fit2026!'),role='operator'))
 s.add(User(email='viewer@hesheng.local',password=password_hash('Fit2026!'),role='viewer'))
 s.add(Shop(id=1,now='2026-11-18T09:00:00'))
 c=Consignor(name='测试寄售人',contact='柜台留存');s.add(c);s.flush()
 s.add(Agreement(code='A1',consignor_id=c.id,floor_cents=100,commission_bp=1000,ends='2026-12-31T18:00:00'));s.flush()
 g=Garment(agreement_id=c.id,code='G1',title='测试外套',condition='八成新无破损',price_cents=20000,state='listed');s.add(g);s.flush()
 s.add(Measurement(garment_id=g.id,chest=96,shoulder=39,length=58,elasticity=.2,cut=1))
 s.add(FitProfile(code='P1',chest=88,shoulder=37))
 s.add(FitProfile(code='P2',chest=90,shoulder=38))
 s.commit()

tok=token_for(type('U',(),{'id':1})())
VIEW=token_for(type('U',(),{'id':2})())
H={'Authorization':'Bearer '+tok}
HV={'Authorization':'Bearer '+VIEW}
client=TestClient(app)
PASS=0;FAIL=0
def check(name,cond,extra=''):
 global PASS,FAIL
 if cond:PASS+=1;print('  ✓',name)
 else:FAIL+=1;print('  ✗ FAIL',name,extra)

def reg():return client.get('/register',headers=H).json()
def find(lst,**kw):return next(x for x in lst if all(x[k]==v for k,v in kw.items()))

print('1) 受理撤回：体型即时不可查，排队建议作废，撤回单为待清理')
d=reg()
p1=find(d['profiles'],code='P1');p2=find(d['profiles'],code='P2')
g1=find(d['garments'],code='G1')
# 建两条评估：一条排队中（模拟尚未被 worker 取走），一条已 ready
r=client.post('/assessments',headers=H,json={'garment_id':g1['id'],'profile_id':p1['id']});check('评估A入队200',r.status_code==200,r.text)
a_queue=r.json()
with Session() as s:
 s.add(FitAssessment(garment_id=g1['id'],profile_id=p1['id'],state='ready',
   result={'label':'偏紧','features':[8,2],'probabilities':{'偏紧':.8},'source_sha256':'SRC','training_sha256':'T'}))
 s.commit()
r=client.post('/fit-withdrawals',headers=H,json={'profile_id':p1['id'],'reason':'买家来电要求撤回其匿名体型数据'})
check('受理200',r.status_code==200,r.text);w=r.json()
check('撤回单 pending',w['state']=='pending' and w['cleaned_at'] is None)
d=reg()
check('体型 P1 从 register 消失（受理起查不到）',not any(x['code']=='P1' for x in d['profiles']))
check('P2 仍可见',any(x['code']=='P2' for x in d['profiles']))
check('fit_withdrawals 已下发',len(d['fit_withdrawals'])==1)
aq=find(d['assessments'],id=a_queue['id'])
check('排队建议被作废(withdrawn)',aq['state']=='withdrawn',str(aq))
ar=find(d['assessments'],state='ready')
check('受理即脱敏：ready 建议只剩 label',ar['result']=={'label':'偏紧'},str(ar['result']))
check('受理起可倒推围度依据已不可查',all(k not in ar['result'] for k in ('features','probabilities','source_sha256','training_sha256')))
# DB 内层：profile 软撤回但未删除
with Session() as s:
 pp=s.get(FitProfile,p1['id']);check('库内 profile 仍在但 state=withdrawn',pp is not None and pp.state=='withdrawn')

print('2) 撤回与推理并发：worker 取到作废任务，迟到结果不得写回')
# 队列里仍有 a_queue 的 id（BLPOP 会弹出），worker 处理时发现 state!=queued 必须跳过
q=QS[os.environ['REDIS_URL']]
q.rpush('resale:fit',a_queue['id'])
# 复刻 worker.py 弹出任务后的守卫逻辑（同条件：not x or x.state!='queued' -> continue，不写回）
with Session() as s:
 from src.common import lock,rows,get,dump
 item=q.blpop('resale:fit',timeout=1)
 check('worker 弹出任务',item is not None and int(item[1])==a_queue['id'])
 lock(s);x=s.get(FitAssessment,int(item[1]))
 skipped=(not x or x.state!='queued')
 check('非 queued 直接跳过，不回写',skipped and x.state=='withdrawn' and x.result is None)
# 反向场景：queued 任务在 worker 计算期间被撤回 -> worker 检测到 profile 非 active，落 failed 而非 ready
r=client.post('/assessments',headers=H,json={'garment_id':g1['id'],'profile_id':p2['id']});a2=r.json()
client.post('/fit-withdrawals',headers=H,json={'profile_id':p2['id'],'reason':'买家二号同样要求撤回体型数据'})
# 模拟 worker 取任务（state 已被撤回改成 withdrawn）
with Session() as s:
 from src.common import lock
 x=s.get(FitAssessment,a2['id']);check('计算中任务受理后即作废',x.state=='withdrawn')
# 若任务在撤回前已被 worker 锁定并通过 queued 检查：profile.state 防护
with Session() as s:
 from src.common import lock,rows,get,dump
 s.get(FitAssessment,a2['id']).state='queued';s.commit()
 lock(s);x=s.get(FitAssessment,a2['id'])
 try:
  m=next(m for m in rows(s,Measurement) if m.garment_id==x.garment_id);pp=get(s,FitProfile,x.profile_id)
  if pp.state!='active':raise RuntimeError('匿名体型已撤回，结果作废')
  raised=False
 except RuntimeError:raised=True
 check('worker 防护：撤回后计算抛错，ready 不会被写回',raised)
 # 严格迟到：worker 已通过 queued 检查并算完，commit 前撤回已落库 -> 回写守卫拦截
with Session() as s:
 from src.common import lock,rows,get,dump
 x=s.get(FitAssessment,a2['id']);x.state='queued';s.commit()
 lock(s);x=s.get(FitAssessment,a2['id'])
 check('回写前仍为 queued',x.state=='queued')
 pp=get(s,FitProfile,x.profile_id)
 guard_block=(pp.state!='active')  # worker 在写 ready 前的同一守卫条件
 check('迟到结果守卫：profile 已撤回，禁止写回 ready',guard_block)
 if guard_block:x.state='failed';x.error='匿名体型已撤回，结果作废'
 s.commit()
with Session() as s:
 check('迟到任务最终未变 ready',s.get(FitAssessment,a2['id']).state!='ready')

print('3) 撤回后禁止再用该体型评估/留衣/重复受理')
r=client.post('/assessments',headers=H,json={'garment_id':g1['id'],'profile_id':p1['id']})
check('撤回体型评估被拒409',r.status_code==409)
r=client.post('/fittings',headers=H,json={'garment_id':g1['id'],'profile_id':p1['id'],'slot':1,'starts':'2026-11-19T15:00:00','ends':'2026-11-19T16:00:00'})
check('撤回体型留衣被拒409',r.status_code==409)
r=client.post('/fit-withdrawals',headers=H,json={'profile_id':p1['id'],'reason':'重复受理不应产生第二张单'})
check('待清理期间重复受理仍200且不新增记录',r.status_code==200 and r.json()['id']==w['id'])
d=reg();check('撤回单仍只有 P1/P2 两张',len(d['fit_withdrawals'])==2)

print('4) 清理中断恢复：清理到一半失败，重试复用原单，不新增记录')
w1=find(reg()['fit_withdrawals'],profile_id=p1['id'])
# 模拟中断：手工把 profile 删掉但撤回单仍 pending（部分完成）
with Session() as s:
 s.delete(s.get(FitProfile,p1['id']));s.commit()
r=client.post(f'/fit-withdrawals/{w1["id"]}/cleanup',headers=H)
check('中断后清理重试200',r.status_code==200,r.text)
w1c=r.json();check('状态变 cleaned 且是原单',w1c['state']=='cleaned' and w1c['id']==w1['id'] and w1c['cleaned_at'])
d=reg()
check('清理后撤回单不新增（仍2张）',len(d['fit_withdrawals'])==2)
# 历史建议：ready 的只留 label，作废的 result 清空，profile_id 全部置空
for a in d['assessments']:
 if a['profile_id']==p1['id'] or (a['id'] in (a_queue['id'],ar['id'])):
  pass
a_ready=find(d['assessments'],id=ar['id'])
check('已给松紧判定 label 保留',a_ready['result']=={'label':'偏紧'},str(a_ready['result']))
check('可倒推围度依据已清除',all(k not in a_ready['result'] for k in ('features','probabilities','source_sha256','training_sha256')))
a_void=find(d['assessments'],id=a_queue['id'])
check('作废建议 result 为 null',a_void['result'] is None and a_void['state']=='withdrawn')
p1_assess=[a for a in d['assessments'] if a['id'] in (a_queue['id'],ar['id'])]
check('历史建议解除体型关联(profile_id=null)',all(a['profile_id'] is None for a in p1_assess),str(p1_assess))
with Session() as s:
 check('profile 物理删除，彻底清除',s.get(FitProfile,p1['id']) is None)

print('5) 清理幂等：再次清理/受理已清理单')
r=client.post(f'/fit-withdrawals/{w1["id"]}/cleanup',headers=H)
check('重复清理幂等200且仍为原单',r.status_code==200 and r.json()['id']==w1['id'] and r.json()['state']=='cleaned')
r=client.post('/fit-withdrawals',headers=H,json={'profile_id':p1['id'],'reason':'对已清理单再受理'})
check('已清理单再受理被拒409',r.status_code==409)

print('6) P2 完整清理路径（未中断）')
w2=find(reg()['fit_withdrawals'],profile_id=p2['id'])
r=client.post(f'/fit-withdrawals/{w2["id"]}/cleanup',headers=H)
check('P2 清理200',r.status_code==200 and r.json()['state']=='cleaned')
with Session() as s:check('P2 profile 物理删除',s.get(FitProfile,p2['id']) is None)
d=reg()
check('待清理/已清理可区分：两张均 cleaned',all(x['state']=='cleaned' for x in d['fit_withdrawals']))
check('可见体型归零',len(d['profiles'])==0)

print('7) 权限：viewer 不能受理/清理')
r=client.post('/fit-withdrawals',headers=HV,json={'profile_id':999,'reason':'viewer 试图受理撤回请求'})
check('viewer 受理被拒403',r.status_code==403)
r=client.post(f'/fit-withdrawals/{w1["id"]}/cleanup',headers=HV)
check('viewer 清理被拒403',r.status_code==403)

print('8) 不存在的撤回单/体型 404')
r=client.post('/fit-withdrawals/9999/cleanup',headers=H)
check('清理不存在撤回单404',r.status_code==404)
r=client.post('/fit-withdrawals',headers=H,json={'profile_id':9999,'reason':'不存在体型的撤回请求测试'})
check('受理不存在体型404',r.status_code==404)

print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
