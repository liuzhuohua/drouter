#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外置存储设备（USB / Type-C / 雷电）的纯逻辑测试。

重点守住「格式化」的三重保护 —— 这是全项目最容易造成不可逆损失的操作：
  1) 系统盘（承载 / /boot /boot/efi 等）禁止格式化；
  2) 已挂载的设备必须先卸载；
  3) 整块盘若已含分区，禁止格式化整盘（应格式化具体分区）；
  4) 前端二次确认必须手输设备名，后端还要再校验一次。

从 drouter-helper.py 抽取相关函数，lsblk / mkfs / mount 全部换成假的。
"""
import ast
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'backend', 'drouter-helper.py')
code = open(SRC, encoding='utf-8').read()
tree = ast.parse(code)

WANT_FN = ['_blk_sysfs', '_dev_bus', '_walk_blk', '_sysfs_model',
           '_dev_is_system', '_fs_meta', '_fs_tool_ready', '_read_fstab',
           '_backup_fstab', '_safe_mnt_seg', '_mnt_target_error', 'act_storage']
WANT_AS = ['FS_TYPES', '_CRITICAL_MOUNTS']

picked = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FN:
        picked.append(node)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in WANT_AS:
                picked.append(node)
got = set()
for n in picked:
    if isinstance(n, ast.Assign):
        got.update(t.id for t in n.targets if isinstance(t, ast.Name))
    else:
        got.add(n.name)
missing = (set(WANT_FN) | set(WANT_AS)) - got
assert not missing, '未能从源码抽取：%s' % ', '.join(sorted(missing))

# ---- 测试替身 -------------------------------------------------------------
STATE = {'devices': [], 'sh_rc': 0, 'sh_out': '', 'sh_calls': []}


def fake_sh(cmd, timeout=None, **kw):
    STATE['sh_calls'].append(cmd)
    return (STATE['sh_rc'], STATE['sh_out'], '')


def fake_log(*a, **k):
    pass


GEN = tempfile.mkdtemp()

def _ok(data=None, msg='操作成功', code='OK'):
    return {'ok': True, 'code': code, 'msg_cn': msg, 'data': data}


def _fail(msg, code='ERR', data=None):
    return {'ok': False, 'code': code, 'msg_cn': msg, 'data': data}


ns = {'os': os, 'json': json, 'shutil': shutil, 'time': __import__('time'),
      're': __import__('re'),
      'sh': fake_sh, 'log': fake_log, 'GEN': GEN,
      'ok': _ok, 'fail': _fail,
      '_storage_device_rows': lambda: STATE['devices'],
      '_hbytes': lambda n: '%d B' % n}
exec(compile('\n'.join(ast.unparse(n) for n in picked), '<x>', 'exec'), ns)

_dev_bus = ns['_dev_bus']
_dev_is_system = ns['_dev_is_system']
_fs_meta = ns['_fs_meta']
_fs_tool_ready = ns['_fs_tool_ready']
act_storage = ns['act_storage']
FS_TYPES = ns['FS_TYPES']

fails = []


def ck(name, cond, extra=''):
    print('  %-46s %s %s' % (name, 'OK  ' if cond else 'FAIL', extra))
    if not cond:
        fails.append(name)


print('=' * 66)
print('一、连接方式判定（总线类型）')
ck('sysfs 含 thunderbolt → 雷电',
   _dev_bus('sdb', '/sys/devices/pci0000:00/0000:00:0d.2/thunderbolt/0-1/nvme') ==
   ('thunderbolt', '雷电 / USB4'))
ck('sysfs 含 /usb → USB',
   _dev_bus('sdc', '/sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/block/sdc') ==
   ('usb', 'USB'))
ck('sysfs 含 nvme → NVMe',
   _dev_bus('nvme0n1', '/sys/devices/pci0000:00/nvme/nvme0/nvme0n1') ==
   ('nvme', 'NVMe'))
ck('sysfs 含 /ata → SATA',
   _dev_bus('sda', '/sys/devices/pci0000:00/ata1/host0/target0:0:0/0:0:0:0/block/sda') ==
   ('sata', 'SATA / SCSI'))
ck('sysfs 含 virtio → 虚拟磁盘',
   _dev_bus('vda', '/sys/devices/pci0000:00/0000:00:05.0/virtio1/block/vda') ==
   ('virtual', '虚拟磁盘'))
ck('mmcblk 命名 → MMC/TF 卡',
   _dev_bus('mmcblk0', '/sys/devices/platform/mmc0/mmcblk0') ==
   ('mmc', 'MMC / TF 卡'))
ck('未知路径 → unknown', _dev_bus('xxx', '/weird/path')[0] == 'unknown')

print('二、系统盘识别（禁止格式化的核心依据）')
rows = [
    {'name': 'sda', 'parent': '', 'partitions': ['sda1', 'sda2'], 'mountpoint': ''},
    {'name': 'sda1', 'parent': 'sda', 'partitions': [], 'mountpoint': '/boot'},
    {'name': 'sda2', 'parent': 'sda', 'partitions': [], 'mountpoint': '/'},
    {'name': 'sdb', 'parent': '', 'partitions': ['sdb1'], 'mountpoint': ''},
    {'name': 'sdb1', 'parent': 'sdb', 'partitions': [], 'mountpoint': ''},
]
ck('sda2（挂载 /）判定为系统盘', _dev_is_system('sda2', rows)[0] is True)
ck('sda1（挂载 /boot）判定为系统盘', _dev_is_system('sda1', rows)[0] is True)
ck('整盘 sda（子分区挂 /）也判为系统盘', _dev_is_system('sda', rows)[0] is True)
ck('外接 sdb1 不是系统盘', _dev_is_system('sdb1', rows)[0] is False)

print('三、文件系统元数据')
ck('FS_TYPES 不少于 7 种', len(FS_TYPES) >= 7, '实际 %d' % len(FS_TYPES))
ck('exFAT 三平台通用',
   all(_fs_meta('exfat')[k] for k in ('win', 'mac', 'linux')))
ck('ext4 仅 Linux', _fs_meta('ext4')['linux'] and not _fs_meta('ext4')['win'])
ck('FAT32 有 4GB 限制说明', '4GB' in _fs_meta('vfat')['note'])
ck('每个 fs 都有 mkfs / pkg / note',
   all(f.get('mkfs') and f.get('pkg') and f.get('note') for f in FS_TYPES))
ck('不支持的 fs 返回 None', _fs_meta('reiser9') is None)

print('四、格式化工具可用性探测（PATH 里找可执行文件）')
tmpbin = tempfile.mkdtemp()
for t in ('mkfs.ext4', 'mkfs.exfat'):
    p = os.path.join(tmpbin, t)
    open(p, 'w').write('#!/bin/sh\n')
    os.chmod(p, 0o755)
old_path = os.environ['PATH']
os.environ['PATH'] = tmpbin
try:
    ck('PATH 里有 mkfs.ext4 → ready', _fs_tool_ready('ext4')[0] is True)
    ck('PATH 里没有 mkfs.ntfs → not ready', _fs_tool_ready('ntfs')[0] is False)
    ck('不可用时给出软件包名', 'ntfs-3g' in _fs_tool_ready('ntfs')[1])
finally:
    os.environ['PATH'] = old_path

print('五、format 的三重保护（最关键）')
EXT = [{'name': 'sdb1', 'type': 'part', 'fstype': '', 'mounted': False,
        'mountpoint': '', 'parts': []}]
STATE['devices'] = [
    {'name': 'sda', 'type': 'disk', 'fstype': '', 'mountpoint': '', 'mounted': False,
     'parent': '', 'partitions': ['sda1'], 'has_children': True},
    {'name': 'sda1', 'type': 'part', 'fstype': 'ext4', 'mountpoint': '/',
     'mounted': True, 'parent': 'sda', 'partitions': [], 'has_children': False},
    {'name': 'sdb', 'type': 'disk', 'fstype': '', 'mountpoint': '', 'mounted': False,
     'parent': '', 'partitions': ['sdb1'], 'has_children': True},
    {'name': 'sdb1', 'type': 'part', 'fstype': '', 'mountpoint': '', 'mounted': False,
     'parent': 'sdb', 'partitions': [], 'has_children': False},
    {'name': 'sdc1', 'type': 'part', 'fstype': 'exfat', 'mountpoint': '/mnt/x',
     'mounted': True, 'parent': '', 'partitions': [], 'has_children': False},
]

r = act_storage({'op': 'format', 'name': 'sda1', 'fs': 'ext4', 'confirm': 'sda1'})
ck('保护1：系统盘拒绝格式化', (not r['ok']) and r['code'] == 'SYSTEM_DISK', r.get('code'))

r = act_storage({'op': 'format', 'name': 'sdc1', 'fs': 'ext4', 'confirm': 'sdc1'})
ck('保护2：已挂载拒绝格式化', (not r['ok']) and r['code'] == 'MOUNTED', r.get('code'))

r = act_storage({'op': 'format', 'name': 'sdb', 'fs': 'ext4', 'confirm': 'sdb'})
ck('保护3：整盘含分区拒绝格式化', (not r['ok']) and r['code'] == 'DISK_HAS_PARTS',
   r.get('code'))

r = act_storage({'op': 'format', 'name': 'sdb1', 'fs': 'ext4', 'confirm': 'sdb'})
ck('保护4：确认名不匹配拒绝', (not r['ok']) and r['code'] == 'NEED_CONFIRM', r.get('code'))

r = act_storage({'op': 'format', 'name': 'sdb1', 'fs': 'ufs', 'confirm': 'sdb1'})
ck('不支持的文件系统被拒', (not r['ok']) and r['code'] == 'BAD_FS', r.get('code'))

# 保持 PATH 里有 mkfs 工具，才能测到「mkfs 本身执行失败」，
# 而不是被「没装工具（NO_TOOL）」提前拦掉、掩盖了真正的分支。
os.environ['PATH'] = tmpbin
STATE['sh_calls'] = []
r = act_storage({'op': 'format', 'name': 'sdb1', 'fs': 'ext4', 'confirm': 'sdb1'})
ck('条件齐备时真正执行 mkfs（不再拦截）',
   r['ok'] and STATE['sh_calls'] and STATE['sh_calls'][-1][0] == 'mkfs.ext4',
   str(STATE['sh_calls'][-1] if STATE['sh_calls'] else ''))

STATE['sh_rc'] = 1
r = act_storage({'op': 'format', 'name': 'sdb1', 'fs': 'exfat', 'confirm': 'sdb1'})
ck('mkfs 执行失败时如实报错（不假装成功）',
   (not r['ok']) and r['code'] == 'FORMAT', r.get('code'))
STATE['sh_rc'] = 0
os.environ['PATH'] = old_path

print('六、挂载 / 卸载')
STATE['sh_calls'] = []
r = act_storage({'op': 'mount', 'name': 'sdb1'})
ck('未格式化的设备不能挂载', (not r['ok']) and r['code'] == 'NOFS', r.get('code'))
r = act_storage({'op': 'mount', 'name': 'sdc1'})
ck('已挂载的设备不能重复挂载', (not r['ok']) and r['code'] == 'ALREADY', r.get('code'))
r = act_storage({'op': 'umount', 'name': 'sdb1'})
ck('未挂载的设备不能卸载', (not r['ok']) and r['code'] == 'NOTMOUNTED', r.get('code'))
STATE['sh_rc'] = 1
r = act_storage({'op': 'umount', 'name': 'sdc1'})
ck('卸载失败时给出排查命令（lsof/fuser）',
   (not r['ok']) and ('lsof' in r['msg_cn']), r.get('code'))
STATE['sh_rc'] = 0

print('七、fstab 开机自动挂载')
# 不测真实写入（那会动 /etc/fstab），只测参数校验与重复检测：
# 这两条最容易出「写进去了但开机起不来」和「同一设备被重复追加两次」。
r = act_storage({'op': 'fstab_add', 'name': 'sda', 'target': '/mnt/x'})
ck('无 UUID 的设备不能写 fstab',
   r.get('code') in ('NOUUID', 'NODEV'), r.get('code'))
r = act_storage({'op': 'fstab_del', 'uuid': ''})
ck('fstab_del 缺少 UUID 时报错', r.get('code') == 'NOUUID', r.get('code'))
# 重复检测：把 /etc/fstab 内容里塞进同一 UUID，应当判定为已存在
ns['_read_fstab'] = lambda: '# test\nUUID=AAAA-1111 /mnt/usb exfat defaults 0 0\n'
for d in STATE['devices']:
    if d['name'] == 'sdb1':
        d['uuid'] = 'AAAA-1111'
        d['fstype'] = 'exfat'
r = act_storage({'op': 'fstab_add', 'name': 'sdb1', 'target': '/mnt/usb'})
ck('同一 UUID 重复添加会被拒绝', r.get('code') == 'DUP', r.get('code'))

print('八、前端必须有的要素')
js = open(os.path.join(HERE, '..', 'web', 'app.js'), encoding='utf-8').read()
for k, label in [('stgLoad', '读取设备列表'), ('stgFormat', '格式化弹窗'),
                 ('stg-confirm', '手输设备名确认'), ('stgShare', '一键加入共享'),
                 ('开机挂载', '开机自动挂载按钮'), ('外置存储设备', '卡片标题')]:
    ck('前端含 %s' % label, k in js)
ck('格式化弹窗会校验确认名再提交',
   'confirm !== d.name' in js)
ck('已挂载时格式化按钮禁用', "d.mounted ? 'disabled" in js or 'disabled title="请先卸载再格式化"' in js)

print('=' * 66)
print('结果：失败 %d 项' % len(fails))
shutil.rmtree(tmpbin, ignore_errors=True)
shutil.rmtree(GEN, ignore_errors=True)
sys.exit(1 if fails else 0)
