# -*- coding: UTF-8 -*- 

import re
import simplejson as json
from threading import Thread
from collections import OrderedDict
import datetime
from django.db.models import Q, F
from django.db import connection, transaction
from django.utils import timezone
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse, HttpResponseRedirect
from django.core.urlresolvers import reverse

from .dao import Dao
from .const import Const, WorkflowDict
from .inception import InceptionDao
from .aes_decryptor import Prpcrypt
from .models import users, master_config, AliyunRdsConfig, workflow, slave_config, QueryPrivileges, Group, \
    QueryPrivilegesApply
from .workflow import Workflow
from .permission import role_required, superuser_required
from .sqlreview import getDetailUrl, execute_call_back, execute_skipinc_call_back
from .jobs import job_info, del_sqlcronjob, add_sqlcronjob
import logging

logger = logging.getLogger('default')

dao = Dao()
inceptionDao = InceptionDao()
prpCryptor = Prpcrypt()
workflowOb = Workflow()


# 登录
def login(request):
    return render(request, 'login.html')


# 退出登录
def logout(request):
    if request.session.get('login_username', False):
        del request.session['login_username']
    return HttpResponseRedirect(reverse('sql:login'))


# SQL上线工单页面
def allworkflow(request):
    context = {'currentMenu': 'allworkflow'}
    return render(request, 'allWorkflow.html', context)


# 提交SQL的页面
def submitSql(request):
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')

    # 获取所有项组名称
    group_list = Group.objects.all().annotate(id=F('group_id'),
                                              name=F('group_name'),
                                              parent=F('group_parent_id'),
                                              level=F('group_level')
                                              ).values('id', 'name', 'parent', 'level')

    group_list = [group for group in group_list]
    if len(group_list) == 0:
        return HttpResponseRedirect('/config/')

    # 获取所有实例名称
    listAllClusterName = [master.cluster_name for master in masters]

    # 获取所有有效用户，通知对象
    active_user = users.objects.filter(is_active=1)

    context = {'currentMenu': 'allworkflow', 'listAllClusterName': listAllClusterName,
               'active_user': active_user, 'group_list': group_list}
    return render(request, 'submitSql.html', context)


# 提交SQL给inception进行解析
def autoreview(request):
    workflowid = request.POST.get('workflowid')
    sqlContent = request.POST['sql_content']
    workflowName = request.POST['workflow_name']
    group_name = request.POST['group_name']
    group_id = Group.objects.get(group_name=group_name).group_id
    clusterName = request.POST['cluster_name']
    db_name = request.POST.get('db_name')
    isBackup = request.POST['is_backup']
    reviewMan = request.POST.get('workflow_auditors')
    notify_users = request.POST.getlist('notify_users')

    # 服务器端参数验证
    if sqlContent is None or workflowName is None or clusterName is None or db_name is None or isBackup is None or reviewMan is None:
        context = {'errMsg': '页面提交参数可能为空'}
        return render(request, 'error.html', context)

    # 删除注释语句
    sqlContent = ''.join(
        map(lambda x: re.compile(r'(^--.*|^/\*.*\*/;[\f\n\r\t\v\s]*$)').sub('', x, count=1),
            sqlContent.splitlines(1))).strip()
    # 去除空行
    sqlContent = re.sub('[\r\n\f]{2,}', '\n', sqlContent)

    if sqlContent[-1] != ";":
        context = {'errMsg': "SQL语句结尾没有以;结尾，请后退重新修改并提交！"}
        return render(request, 'error.html', context)

    # 交给inception进行自动审核
    try:
        result = inceptionDao.sqlautoReview(sqlContent, clusterName, db_name)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)

    if result is None or len(result) == 0:
        context = {'errMsg': 'inception返回的结果集为空！可能是SQL语句有语法错误'}
        return render(request, 'error.html', context)
    # 要把result转成JSON存进数据库里，方便SQL单子详细信息展示
    jsonResult = json.dumps(result)

    # 遍历result，看是否有任何自动审核不通过的地方，一旦有，则为自动审核不通过；没有的话，则为等待人工审核状态
    workflowStatus = Const.workflowStatus['manreviewing']
    for row in result:
        if row[2] == 2:
            # 状态为2表示严重错误，必须修改
            workflowStatus = Const.workflowStatus['autoreviewwrong']
            break
        elif re.match(r"\w*comments\w*", row[4]):
            workflowStatus = Const.workflowStatus['autoreviewwrong']
            break

    # 调用工作流生成工单
    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 存进数据库里
            engineer = request.session.get('login_username', False)
            if not workflowid:
                Workflow = workflow()
                Workflow.create_time = timezone.now()
            else:
                Workflow = workflow.objects.get(id=int(workflowid))
            Workflow.workflow_name = workflowName
            Workflow.group_id = group_id
            Workflow.group_name = group_name
            Workflow.engineer = engineer
            Workflow.review_man = reviewMan
            Workflow.status = workflowStatus
            Workflow.is_backup = isBackup
            Workflow.review_content = jsonResult
            Workflow.cluster_name = clusterName
            Workflow.db_name = db_name
            Workflow.sql_content = sqlContent
            Workflow.execute_result = ''
            Workflow.audit_remark = ''
            Workflow.save()
            workflowId = Workflow.id
            # 自动审核通过了，才调用工作流
            if workflowStatus == Const.workflowStatus['manreviewing']:
                # 调用工作流插入审核信息, 查询权限申请workflow_type=2
                # 抄送通知人
                listCcAddr = [email['email'] for email in
                              users.objects.filter(username__in=notify_users).values('email')]
                workflowOb.addworkflowaudit(request, WorkflowDict.workflow_type['sqlreview'], workflowId,
                                            listCcAddr=listCcAddr)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)

    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 展示SQL工单详细内容，以及可以人工审核，审核通过即可执行
def detail(request, workflowId):
    workflowDetail = get_object_or_404(workflow, pk=workflowId)
    if workflowDetail.status in (Const.workflowStatus['finish'], Const.workflowStatus['exception']) \
            and workflowDetail.is_manual == 0:
        listContent = json.loads(workflowDetail.execute_result)
    else:
        listContent = json.loads(workflowDetail.review_content)

    # 获取审核人
    reviewMan = workflowDetail.review_man
    reviewMan = reviewMan.split(',')

    # 获取当前审核人
    try:
        current_audit_user = workflowOb.auditinfobyworkflow_id(workflow_id=workflowId,
                                                               workflow_type=WorkflowDict.workflow_type['sqlreview']
                                                               ).current_audit_user
    except Exception:
        current_audit_user = None

    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取定时执行任务信息
    if workflowDetail.status == Const.workflowStatus['timingtask']:
        job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)
        job = job_info(job_id)
        if job:
            run_date = job.next_run_time
        else:
            run_date = ''
    else:
        run_date = ''

    # sql结果
    column_list = ['ID', 'stage', 'errlevel', 'stagestatus', 'errormessage', 'SQL', 'Affected_rows', 'sequence',
                   'backup_dbname', 'execute_time', 'sqlsha1']
    rows = []
    for row_index, row_item in enumerate(listContent):
        row = {}
        row['ID'] = row_index + 1
        row['stage'] = row_item[1]
        row['errlevel'] = row_item[2]
        row['stagestatus'] = row_item[3]
        row['errormessage'] = row_item[4]
        row['SQL'] = row_item[5]
        row['Affected_rows'] = row_item[6]
        row['sequence'] = row_item[7]
        row['backup_dbname'] = row_item[8]
        row['execute_time'] = row_item[9]
        row['sqlsha1'] = row_item[10]
        rows.append(row)

        if workflowDetail.status == '执行中':
            row['stagestatus'] = ''.join(
                ["<div id=\"td_" + str(row['ID']) + "\" class=\"form-inline\">",
                 "   <div class=\"progress form-group\" style=\"width: 80%; height: 18px; float: left;\">",
                 "       <div id=\"div_" + str(row['ID']) + "\" class=\"progress-bar\" role=\"progressbar\"",
                 "            aria-valuenow=\"60\"",
                 "            aria-valuemin=\"0\" aria-valuemax=\"100\">",
                 "           <span id=\"span_" + str(row['ID']) + "\"></span>",
                 "       </div>",
                 "   </div>",
                 "   <div class=\"form-group\" style=\"width: 10%; height: 18px; float: right;\">",
                 "       <form method=\"post\">",
                 "           <input type=\"hidden\" name=\"workflowid\" value=\"" + str(workflowDetail.id) + "\">",
                 "           <button id=\"btnstop_" + str(row['ID']) + "\" value=\"" + str(row['ID']) + "\"",
                 "                   type=\"button\" class=\"close\" style=\"display: none\" title=\"停止pt-OSC进程\">",
                 "               <span class=\"glyphicons glyphicons-stop\">&times;</span>",
                 "           </button>",
                 "       </form>",
                 "   </div>",
                 "</div>"])
    context = {'currentMenu': 'allworkflow', 'workflowDetail': workflowDetail, 'column_list': column_list, 'rows': rows,
               'reviewMan': reviewMan, 'current_audit_user': current_audit_user, 'loginUserOb': loginUserOb,
               'run_date': run_date}
    return render(request, 'detail.html', context)


# 审核通过，不执行
def passonly(request):
    workflowId = request.POST['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)
    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)

    # 获取审核人
    reviewMan = workflowDetail.review_man
    reviewMan = reviewMan.split(',')

    # 服务器端二次验证，正在执行人工审核动作的当前登录用户必须为审核人. 避免攻击或被接口测试工具强行绕过
    loginUser = request.session.get('login_username', False)
    if loginUser is None or loginUser not in reviewMan:
        context = {'errMsg': '当前登录用户不是审核人，请重新登录.'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，当前工单状态必须为等待人工审核
    if workflowDetail.status != Const.workflowStatus['manreviewing']:
        context = {'errMsg': '当前工单状态不是等待人工审核中，请刷新当前页面！'}
        return render(request, 'error.html', context)

    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 调用工作流接口审核
            # 获取audit_id
            audit_id = workflowOb.auditinfobyworkflow_id(workflow_id=workflowId,
                                                         workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
            auditresult = workflowOb.auditworkflow(request, audit_id, WorkflowDict.workflow_status['audit_success'],
                                                   loginUser, '')

            # 按照审核结果更新业务表审核状态
            if auditresult['data']['workflow_status'] == WorkflowDict.workflow_status['audit_success']:
                # 将流程状态修改为审核通过，并更新reviewok_time字段
                workflowDetail.status = Const.workflowStatus['pass']
                workflowDetail.reviewok_time = timezone.now()
                workflowDetail.audit_remark = ''
                workflowDetail.save()
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)

    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 仅执行SQL
def executeonly(request):
    workflowId = request.POST['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)

    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)
    clusterName = workflowDetail.cluster_name
    db_name = workflowDetail.db_name
    url = getDetailUrl(request) + str(workflowId) + '/'

    # 获取审核人
    reviewMan = workflowDetail.review_man
    reviewMan = reviewMan.split(',')

    # 服务器端二次验证，正在执行人工审核动作的当前登录用户必须为审核人或者提交人. 避免攻击或被接口测试工具强行绕过
    loginUser = request.session.get('login_username', False)
    if loginUser is None or (loginUser not in reviewMan and loginUser != workflowDetail.engineer):
        context = {'errMsg': '当前登录用户不是审核人或者提交人，请重新登录.'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，当前工单状态必须为审核通过状态
    if workflowDetail.status != Const.workflowStatus['pass']:
        context = {'errMsg': '当前工单状态不是审核通过，请刷新当前页面！'}
        return render(request, 'error.html', context)

    # 将流程状态修改为执行中，并更新reviewok_time字段
    workflowDetail.status = Const.workflowStatus['executing']
    workflowDetail.reviewok_time = timezone.now()
    # 执行之前重新split并check一遍，更新SHA1缓存；因为如果在执行中，其他进程去做这一步操作的话，会导致inception core dump挂掉
    try:
        splitReviewResult = inceptionDao.sqlautoReview(workflowDetail.sql_content, workflowDetail.cluster_name, db_name,
                                                       isSplit='yes')
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    workflowDetail.review_content = json.dumps(splitReviewResult)
    try:
        workflowDetail.save()
    except Exception:
        # 关闭后重新获取连接，防止超时
        connection.close()
        workflowDetail.save()

    # 采取异步回调的方式执行语句，防止出现持续执行中的异常
    t = Thread(target=execute_call_back, args=(workflowId, clusterName, url))
    t.start()

    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 定时执行SQL
@role_required(('DBA',))
def timingtask(request):
    workflowId = request.POST.get('workflowid')
    run_date = request.POST.get('run_date')
    if run_date is None or workflowId is None:
        context = {'errMsg': '时间不能为空'}
        return render(request, 'error.html', context)
    elif run_date < datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'):
        context = {'errMsg': '时间不能小于当前时间'}
        return render(request, 'error.html', context)
    workflowDetail = workflow.objects.get(id=workflowId)
    if workflowDetail.status not in [Const.workflowStatus['pass'], Const.workflowStatus['timingtask']]:
        context = {'errMsg': '必须为审核通过或者定时执行状态'}
        return render(request, 'error.html', context)

    run_date = datetime.datetime.strptime(run_date, "%Y-%m-%d %H:%M:%S")
    url = getDetailUrl(request) + str(workflowId) + '/'
    job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)

    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 将流程状态修改为定时执行
            workflowDetail.status = Const.workflowStatus['timingtask']
            workflowDetail.save()
            # 调用添加定时任务
            add_sqlcronjob(job_id, run_date, workflowId, url)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 跳过inception直接执行SQL，只是为了兼容inception不支持的语法，谨慎使用
@role_required(('DBA',))
def execute_skipinc(request):
    workflowId = request.POST['workflowid']

    # 获取工单信息
    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)
    sql_content = workflowDetail.sql_content
    clusterName = workflowDetail.cluster_name
    url = getDetailUrl(request) + str(workflowId) + '/'

    # 服务器端二次验证，当前工单状态必须为自动审核不通过
    if workflowDetail.status not in [Const.workflowStatus['manreviewing'], Const.workflowStatus['pass'],
                                     Const.workflowStatus['autoreviewwrong']]:
        context = {'errMsg': '当前工单状态不是自动审核不通过，请刷新当前页面！'}
        return render(request, 'error.html', context)

    # 更新工单状态为执行中
    workflowDetail = workflow.objects.get(id=workflowId)
    workflowDetail.status = Const.workflowStatus['executing']
    workflowDetail.reviewok_time = timezone.now()
    workflowDetail.save()

    # 采取异步回调的方式执行语句，防止出现持续执行中的异常
    t = Thread(target=execute_skipinc_call_back, args=(workflowId, clusterName, sql_content, url))
    t.start()

    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 终止流程
def cancel(request):
    workflowId = request.POST['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)

    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)

    # 获取审核人
    reviewMan = workflowDetail.review_man
    reviewMan = reviewMan.split(',')

    audit_remark = request.POST.get('audit_remark')
    if audit_remark is None:
        context = {'errMsg': '驳回原因不能为空'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，如果正在执行终止动作的当前登录用户，不是提交人也不是审核人，则异常.
    loginUser = request.session.get('login_username', False)
    if loginUser is None or (loginUser not in reviewMan and loginUser != workflowDetail.engineer):
        context = {'errMsg': '当前登录用户不是审核人也不是提交人，请重新登录.'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，如果当前单子状态是结束状态，则不能发起终止
    if workflowDetail.status in (
            Const.workflowStatus['abort'], Const.workflowStatus['finish'], Const.workflowStatus['autoreviewwrong'],
            Const.workflowStatus['exception']):
        return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))

    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 调用工作流接口取消或者驳回
            # 获取audit_id
            audit_id = workflowOb.auditinfobyworkflow_id(workflow_id=workflowId,
                                                         workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
            if loginUser == workflowDetail.engineer:
                auditresult = workflowOb.auditworkflow(request, audit_id, WorkflowDict.workflow_status['audit_abort'],
                                                       loginUser, audit_remark)
            else:
                auditresult = workflowOb.auditworkflow(request, audit_id, WorkflowDict.workflow_status['audit_reject'],
                                                       loginUser, audit_remark)
            # 删除定时执行job
            if workflowDetail.status == Const.workflowStatus['timingtask']:
                job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)
                del_sqlcronjob(job_id)
            # 按照审核结果更新业务表审核状态
            if auditresult['data']['workflow_status'] in (
                    WorkflowDict.workflow_status['audit_abort'], WorkflowDict.workflow_status['audit_reject']):
                # 将流程状态修改为人工终止流程
                workflowDetail.status = Const.workflowStatus['abort']
                workflowDetail.audit_remark = audit_remark
                workflowDetail.save()
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 展示回滚的SQL
def rollback(request):
    workflowId = request.GET['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)
    workflowId = int(workflowId)
    try:
        listBackupSql = inceptionDao.getRollbackSqlList(workflowId)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    workflowDetail = workflow.objects.get(id=workflowId)
    workflowName = workflowDetail.workflow_name
    rollbackWorkflowName = "【回滚工单】原工单Id:%s ,%s" % (workflowId, workflowName)
    context = {'listBackupSql': listBackupSql, 'currentMenu': 'sqlworkflow', 'workflowDetail': workflowDetail,
               'rollbackWorkflowName': rollbackWorkflowName}
    return render(request, 'rollback.html', context)


# SQL审核必读
def dbaprinciples(request):
    context = {'currentMenu': 'dbaprinciples'}
    return render(request, 'dbaprinciples.html', context)


# 图表展示
def charts(request):
    context = {'currentMenu': 'charts'}
    return render(request, 'charts.html', context)


# SQL在线查询
def sqlquery(request):
    # 获取所有从库实例名称
    slaves = slave_config.objects.all().order_by('cluster_name')
    if len(slaves) == 0:
        return HttpResponseRedirect('/admin/sql/slave_config/add/')
    listAllClusterName = [slave.cluster_name for slave in slaves]

    context = {'currentMenu': 'sqlquery', 'listAllClusterName': listAllClusterName}
    return render(request, 'sqlquery.html', context)


# SQL慢日志
def slowquery(request):
    # 获取所有实例主库名称
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'slowquery', 'tab': 'slowquery', 'cluster_name_list': cluster_name_list}
    return render(request, 'slowquery.html', context)


# SQL优化工具
def sqladvisor(request):
    # 获取所有实例主库名称
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'sqladvisor', 'listAllClusterName': cluster_name_list}
    return render(request, 'sqladvisor.html', context)


# 查询权限申请列表
def queryapplylist(request):
    slaves = slave_config.objects.all().order_by('cluster_name')
    # 获取所有实例从库名称
    listAllClusterName = [slave.cluster_name for slave in slaves]
    if len(slaves) == 0:
        return HttpResponseRedirect('/admin/sql/slave_config/add/')

    # 获取所有项组名称
    group_list = Group.objects.all().annotate(id=F('group_id'),
                                              name=F('group_name'),
                                              parent=F('group_parent_id'),
                                              level=F('group_level')
                                              ).values('id', 'name', 'parent', 'level')

    group_list = [group for group in group_list]
    if len(group_list) == 0:
        return HttpResponseRedirect('/config/')

    context = {'currentMenu': 'queryapply', 'listAllClusterName': listAllClusterName,
               'group_list': group_list}
    return render(request, 'queryapplylist.html', context)


# 查询权限申请详情
def queryapplydetail(request, apply_id):
    workflowDetail = QueryPrivilegesApply.objects.get(apply_id=apply_id)
    # 获取当前审核人
    audit_info = workflowOb.auditinfobyworkflow_id(workflow_id=apply_id,
                                                   workflow_type=WorkflowDict.workflow_type['query'])

    context = {'currentMenu': 'queryapply', 'workflowDetail': workflowDetail, 'audit_info': audit_info}
    return render(request, 'queryapplydetail.html', context)


# 用户的查询权限管理
def queryuserprivileges(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    # 获取所有用户
    user_list = QueryPrivileges.objects.filter(is_deleted=0).values('user_name').distinct()
    context = {'currentMenu': 'queryapply', 'user_list': user_list, 'loginUserOb': loginUserOb}
    return render(request, 'queryuserprivileges.html', context)


# 问题诊断--进程
def diagnosis_process(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取所有实例名称
    masters = AliyunRdsConfig.objects.all().order_by('cluster_name')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'diagnosis', 'tab': 'process', 'cluster_name_list': cluster_name_list,
               'loginUserOb': loginUserOb}
    return render(request, 'diagnosis.html', context)


# 问题诊断--空间
def diagnosis_sapce(request):
    # 获取所有实例名称
    masters = AliyunRdsConfig.objects.all().order_by('cluster_name')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'diagnosis', 'tab': 'space', 'cluster_name_list': cluster_name_list}
    return render(request, 'diagnosis.html', context)


# 获取工作流审核列表
def workflows(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    context = {'currentMenu': 'workflow', "loginUserOb": loginUserOb}
    return render(request, "workflow.html", context)


# 工作流审核列表
def workflowsdetail(request, audit_id):
    # 按照不同的workflow_type返回不同的详情
    auditInfo = workflowOb.auditinfo(audit_id)
    if auditInfo.workflow_type == WorkflowDict.workflow_type['query']:
        return HttpResponseRedirect(reverse('sql:queryapplydetail', args=(auditInfo.workflow_id,)))
    elif auditInfo.workflow_type == WorkflowDict.workflow_type['sqlreview']:
        return HttpResponseRedirect(reverse('sql:detail', args=(auditInfo.workflow_id,)))


# 配置管理
@superuser_required
def config(request):
    # 获取所有项组名称
    group_list = Group.objects.all().annotate(id=F('group_id'),
                                              name=F('group_name'),
                                              parent=F('group_parent_id'),
                                              level=F('group_level')
                                              ).values('id', 'name', 'parent', 'level')

    group_list = [group for group in group_list]

    # 获取所有用户
    user_list = users.objects.filter(is_active=1).values('username', 'display')
    context = {'currentMenu': 'config', 'group_list': group_list, 'user_list': user_list,
               'WorkflowDict': WorkflowDict}
    return render(request, 'config.html', context)
