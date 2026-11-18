from .models import *
from .common import *
from .constants import ASSESSMENT_VOID_STATE,ASSESSMENT_KEPT_KEYS

def _find(s,profile_id):
 return s.scalars(select(FitWithdrawal).where(FitWithdrawal.profile_id==profile_id)).first()

def _redact(a):
 """已给出的松紧判定（label）留着查，能倒推围度的特征/概率/哈希等依据彻底剥离。"""
 if isinstance(a.result,dict):
  a.result={k:v for k,v in a.result.items() if k in ASSESSMENT_KEPT_KEYS}

def request(s,d):
 """受理买家撤回：撤回单落簿（幂等复用，不新增记录），体型与可倒推围度的依据即时不可查，
 排队/计算中的建议全部作废。"""
 c=lock(s)
 # 已清理（含体型已物理删除）的撤回单优先拦截，保证重复受理语义稳定，不新增记录
 w0=_find(s,d['profile_id'])
 if w0 is not None and w0.state=='cleaned':fail('该体型已完成撤回清理，无需重复受理')
 p=get(s,FitProfile,d['profile_id'])
 w=w0
 if w is None:
  w=FitWithdrawal(profile_id=p.id,profile_code=p.code,reason=d['reason'],requested_at=c.now,state='pending');s.add(w)
 else:
  w.reason=d['reason']  # 同一张待清理撤回单，更新理由仍不新增记录
 # 受理起即查不到：软撤回，列表与下拉均过滤 withdrawn
 p.state='withdrawn'
 # 撤回与推理并发：排队和正在算的建议一律作废；已完成的历史建议受理即脱敏，只留松紧判定
 for a in rows(s,FitAssessment):
  if a.profile_id!=p.id:continue
  if a.state in ('queued','failed'):
   a.state=ASSESSMENT_VOID_STATE;a.error='买家撤回，建议已作废';a.result=None
  else:
   _redact(a)
 s.flush();return w

def cleanup(s,id):
 """按撤回单执行清理，可中断恢复、可重复重试；始终复用原撤回单，不新增记录。

 中断可能发生在任意一步之后（评估脱敏完成但 profile 未删，或 profile 已删但关联未解除），
 因此每一步都按撤回单上的 profile_id 独立判断、可重入，不依赖 profile 是否仍存在。
 受理阶段已剥离依据，这里再做一次幂等脱敏兜底。
 """
 c=lock(s);w=get(s,FitWithdrawal,id)
 if w.state=='cleaned':return w  # 重试幂等：已清理直接返回原单
 pid=w.profile_id
 for a in rows(s,FitAssessment):
  if a.profile_id!=pid:continue
  if a.state==ASSESSMENT_VOID_STATE:
   a.result=None  # 作废建议不保留任何依据
  else:
   _redact(a)
 # 彻底清除体型；profile 可能在中断后的重试中已被删除
 p=s.get(FitProfile,pid)
 if p is not None:s.delete(p)
 # 解除外键后保留历史建议行：profile_id 置空，仅留松紧判定，无法再关联到体型
 for a in rows(s,FitAssessment):
  if a.profile_id==pid:a.profile_id=None
 w.state='cleaned';w.cleaned_at=c.now;s.flush();return w
