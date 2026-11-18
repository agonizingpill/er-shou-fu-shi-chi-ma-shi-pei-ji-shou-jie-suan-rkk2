from .models import *
from .common import *
def measure(s,d):
 c=lock(s);g=garment(s,d['garment_id']);a=get(s,Agreement,g.agreement_id)
 if g.state not in ['unmeasured','listed'] or time(c.now)>=time(a.ends):fail('仅在有效寄售期未留衣前可实测')
 if any(x.garment_id==g.id for x in rows(s,Measurement)):fail('实测已登记，保留原记录')
 x=Measurement(**d);s.add(x);g.state='listed';s.flush();return x
def profile(s,d):
 lock(s)
 if any(x.code==d['code'] and x.state=='active' for x in rows(s,FitProfile)):fail('匿名编号已存在')
 x=FitProfile(**d);s.add(x);s.flush();return x
def assess(s,d):
 lock(s);g=garment(s,d['garment_id']);p=get(s,FitProfile,d['profile_id'])
 if p.state!='active':fail('该匿名体型已撤回，不得再计算适配')
 if not any(x.garment_id==g.id for x in rows(s,Measurement)):fail('未实测服饰不得推荐尺码')
 if g.state not in ['listed','held']:fail('服饰不在可试穿状态')
 x=FitAssessment(**d);s.add(x);s.flush();return x
