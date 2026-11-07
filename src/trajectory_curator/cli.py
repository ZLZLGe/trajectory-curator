import argparse

from .acquire import download_plan, inventory, screen
from .common import dumps
from .delivery import export, verify
from .grading import grade
from .selection import select


def main():
    parser = argparse.ArgumentParser(description='下载、筛选并标注 BaseTraj 轨迹的真实用户需求和难度')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('inventory', help='固定 HF revision 并按 harness/长度选分片')
    p.add_argument('--repo', default='Traj2env/BaseTraj')
    p.add_argument('--revision', default='main')
    p.add_argument('--platform', default='linux', choices=['linux'])
    p.add_argument('--output', required=True)
    p.add_argument('--rows-per-bucket', type=int, default=2500)
    p = commands.add_parser('download', help='下载计划中的分片，验证 SHA256')
    p.add_argument('--plan', required=True)
    p.add_argument('--cache', required=True)
    p.add_argument('--workers', type=int, default=8)
    p = commands.add_parser('screen', help='逐条检查主/子代理、compact、工具质量并抽取用户轮次')
    p.add_argument('--plan', required=True)
    p.add_argument('--cache', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--workers', type=int, default=4)
    p = commands.add_parser('select', help='优先完整记录，按会话/用户文本去重并分层选样')
    p.add_argument('--candidates', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--count', type=int, default=1750)
    p.add_argument('--seed', default='trajectory-curator-v1')
    p = commands.add_parser('grade', help='调用模型分析用户需求、子任务和难度，凭据来自 CURATOR_API_KEY')
    p.add_argument('--candidates', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--endpoint', required=True)
    p.add_argument('--model', default='glm-5.3')
    p.add_argument('--reasoning-effort', default='high', choices=['low', 'high', 'max'])
    p.add_argument('--workers', type=int, default=16)
    p.add_argument('--requests-per-minute', type=int, default=60)
    p.add_argument('--limit', type=int)
    p = commands.add_parser('export', help='剔除标注失败/待复核项并导出原始轨迹和标注侧车')
    p.add_argument('--candidates', required=True)
    p.add_argument('--grades', required=True)
    p.add_argument('--cache', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--count', type=int, default=1500)
    p.add_argument('--plan', required=True)
    p = commands.add_parser('verify', help='逐条复算筛选、轮数、模型输入哈希和难度分数')
    p.add_argument('--output', required=True)
    args = vars(parser.parse_args())
    command = args.pop('command')
    dispatch = {'inventory': inventory, 'download': download_plan, 'screen': screen, 'select': select,
                'grade': grade, 'export': export, 'verify': verify}
    if command in ('download', 'screen'):
        args['plan_path'] = args.pop('plan')
    result = dispatch[command](**args)
    if command in ('grade', 'export', 'verify'):
        print(dumps(result), flush=True)


if __name__ == '__main__':
    main()
