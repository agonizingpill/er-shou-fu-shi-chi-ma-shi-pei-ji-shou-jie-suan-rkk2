ROLES=('operator','supervisor','viewer')
GARMENT_STATES=('unmeasured','listed','held','sold','inspection','withdrawn')
FIT_LABELS=('合身','偏紧','偏松')
PROFILE_STATES=('active','withdrawn')
FIT_WITHDRAWAL_STATES=('pending','cleaned')
ASSESSMENT_VOID_STATE='withdrawn'
# 清理后允许保留的字段：label 是已给出的松紧判定，其余可倒推围度的依据一律删除
ASSESSMENT_KEPT_KEYS=('label',)
