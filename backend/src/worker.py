import os,time,pathlib,logging
import redis
from .database import Session
from .models import FitAssessment,Measurement,FitProfile
from .common import lock,rows,get,dump
from .fit_model import predict
q=redis.from_url(os.environ['REDIS_URL'],decode_responses=True,socket_connect_timeout=1,socket_timeout=4)
while True:
 pathlib.Path('/artifacts/resale-heartbeat').write_text(str(time.time()))
 try:item=q.blpop('resale:fit',timeout=2)
 except redis.RedisError:
  logging.warning('Redis interrupted, retrying');time.sleep(1);continue
 if not item:continue
 with Session() as s:
  lock(s);x=s.get(FitAssessment,int(item[1]))
  # 撤回受理会把排队/计算中的建议置为 withdrawn：迟到结果一律不得写回
  if not x or x.state!='queued':continue
  try:
   m=next(m for m in rows(s,Measurement) if m.garment_id==x.garment_id);p=get(s,FitProfile,x.profile_id)
   if p.state!='active':raise RuntimeError('匿名体型已撤回，结果作废')
   x.result=predict(dump(m),dump(p));x.state='ready'
  except Exception as e:x.state='failed';x.error=str(e)[:500];logging.exception('fit failed')
  s.commit()
