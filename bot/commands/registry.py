"""Command registration and compatibility entrypoint."""

from .admin import *  # noqa: F401,F403
from .capabilities import *  # noqa: F401,F403
from .fun import *  # noqa: F401,F403
from .media import *  # noqa: F401,F403
from .moderation import *  # noqa: F401,F403
from .queries import *  # noqa: F401,F403
from .system import *  # noqa: F401,F403
from .uapi_extra import *  # noqa: F401,F403

def register_all(d):
    d.register("功能", cmd_feature_center, "按分类开关 NapCat 扩展功能 /功能 分类 on/off", bot_owner=True)
    d.register("消息", cmd_message_center, "消息扩展：表情详情、语音转文字、闪传")
    d.register("群管", cmd_group_management_center, "群管扩展：群详情、打卡列表、群备注、批量踢",
               admin_only=True, bot_admin_required=True)
    d.register("待办", cmd_todo_center, "群待办：回复消息添加、完成或取消")
    d.register("相册", cmd_album_center, "群相册：列表、内容、上传、评论、点赞、删除")
    d.register("文件", cmd_file_center, "文件中心：群文件和在线文件")
    d.register("好友", cmd_friend_center, "好友管理：单向好友、备注、删除、可疑申请",
               bot_owner_only=True)
    d.register("账号", cmd_account_center, "账号资料、在线状态和自定义表情",
               bot_owner_only=True)
    d.register("互动", cmd_interaction_center, "分享、闪传等互动能力")
    d.register("自动化", cmd_automation_center, "本群自动化开关", admin_only=True)
    d.register("实验", cmd_experimental_center, "实验性 NapCat 能力", bot_owner_only=True)
    d.register("确认", cmd_confirm_action, "确认一分钟内的高风险操作", admin_only=True)
    d.register("取消确认", cmd_cancel_confirmation, "取消待确认的高风险操作", admin_only=True)
    d.register("api", cmd_api_status, "查看 NapCat/OneBot API 能力状态")
    d.register("群", cmd_target_group_help, "私聊查看目标群帮助 /群 群号 帮助")
    d.register("群信息", cmd_group_info, "查看群信息")
    d.register("成员", cmd_member_info, "查看群成员信息 /成员 QQ号")
    d.register("成员列表", cmd_member_list, "查看群成员列表 /成员列表 [关键词]")
    d.register("文件状态", cmd_file_system_info, "查看群文件存储状态")
    d.register("图片描述", cmd_image_description, "描述图片内容（发送图片或回复图片）")
    d.register("表情回应", cmd_message_reaction, "给回复的消息添加表情 /表情回应 emoji_id")
    d.register("戳", cmd_poke_user, "戳一戳用户 /戳 QQ号")
    d.register("陌生人信息", cmd_stranger_info, "查看 QQ 资料 /陌生人信息 QQ号")
    d.register("好友列表", cmd_friend_list, "查看机器人好友列表", bot_owner_only=True)
    d.register("删除文件", cmd_delete_group_file, "删除群文件 /删除文件 file_id busid",
               admin_only=True, bot_admin_required=True)
    d.register("新建文件夹", cmd_create_group_folder, "新建群文件夹 /新建文件夹 名称",
               admin_only=True, bot_admin_required=True)
    d.register("删除文件夹", cmd_delete_group_folder, "删除群文件夹 /删除文件夹 folder_id",
               admin_only=True, bot_admin_required=True)
    d.register("移动文件", cmd_move_group_file, "移动群文件 /移动文件 file_id 当前目录 目标目录",
               admin_only=True, bot_admin_required=True)
    d.register("重命名文件", cmd_rename_group_file, "重命名群文件 /重命名文件 file_id 当前目录 新名称",
               admin_only=True, bot_admin_required=True)
    d.register("删公告", cmd_delete_group_notice, "删除群公告 /删公告 notice_id",
               admin_only=True, bot_admin_required=True)
    # Basic commands
    d.register("help", cmd_help, "查看可用命令")
    d.register("like", cmd_like, "给用户点赞")
    d.register("rank", cmd_rank, "查看发言排行")
    d.register("weather", cmd_weather, "查询天气 /weather 城市")
    d.register("translate", cmd_translate, "翻译文本 /translate 文本")
    d.register("calc", cmd_calc, "计算器 /calc 1+2*3")
    d.register("fortune", cmd_fortune, "今日运势 /fortune")
    d.register("ocr", cmd_ocr, "识别图片文字 /ocr 或回复图片")
    d.register("转发摘要", cmd_forward_summary, "总结合并转发 /转发摘要")
    d.register("群文件", cmd_group_files, "查看群文件 /群文件 [关键词]")
    d.register("文件链接", cmd_group_file_url, "获取群文件链接 /文件链接 file_id busid")
    d.register("精华列表", cmd_essence_list, "查看群精华")
    d.register("群荣誉", cmd_group_honor, "查看群荣誉")
    d.register("已读", cmd_mark_read, "标记消息已读")
    d.register("history", cmd_history, "查看最近消息 /history [数量]")
    d.register("禁言列表", cmd_shut_list, "查看当前被禁言的人")
    d.register("info", cmd_info, "查看成员信息 /info [@用户] 或 /info QQ号")
    d.register("转发", cmd_forward_msg, "转发消息 (回复消息使用)")
    d.register("setgroupavatar", cmd_set_group_avatar, "设置群头像 (回复图片)",
               admin_only=True, bot_admin_required=True)
    d.register("sysmsg", cmd_sysmsg, "查看入群申请/邀请列表", bot_owner=True)
    d.register("点赞信息", cmd_profile_like, "查看机器人点赞统计")
    d.register("health", cmd_health, "查看运行状态")
    d.register("生图", cmd_generate_image, "AI 生成图片 /生图 提示词")
    d.register("安全", cmd_security, "安全功能 /安全 status|log|url on/off|gray on/off",
               admin_only=True)
    # Admin commands (require bot to be admin/owner)
    d.register("kick", cmd_kick, "踢出成员 /kick @用户",
               admin_only=True, bot_admin_required=True)
    d.register("ban", cmd_ban, "禁言成员 /ban @用户 [分钟]",
               admin_only=True, bot_admin_required=True)
    d.register("unban", cmd_unban, "解除禁言 /unban @用户",
               admin_only=True, bot_admin_required=True)
    d.register("allban", cmd_allban, "全员禁言开关 /allban on/off",
               admin_only=True, bot_admin_required=True)
    d.register("welcome", cmd_welcome, "入群欢迎设置",
               admin_only=True, bot_admin_required=True)
    d.register("badword", cmd_badword, "违禁词设置",
               admin_only=True, bot_admin_required=True)
    d.register("精华", cmd_set_essence, "把回复的消息设为精华",
               admin_only=True, bot_admin_required=True)
    d.register("删精华", cmd_delete_essence, "删除精华消息",
               admin_only=True, bot_admin_required=True)
    d.register("公告", cmd_group_notice, "发布/查看群公告",
               admin_only=True, bot_admin_required=True)
    d.register("clearai", cmd_clear_ai, "清除本群机器人数据",
               bot_owner=True)
    d.register("admin", cmd_admin_mgr, "设置或取消群管理员 /admin add/del @用户",
               admin_only=True, bot_admin_required=True)
    d.register("title", cmd_special_title, "设置专属头衔 /title @用户 头衔",
               admin_only=True, bot_owner_required=True)
    d.register("头衔", cmd_special_title, "设置专属头衔 /头衔 @用户 头衔",
               admin_only=True, bot_owner_required=True)
    # Master management (bot_owner only)
    d.register("master", cmd_master, "管理群主人 /master add/del/list",
               bot_owner_only=True)
    d.register("approve", cmd_approve_request, "同意好友/入群请求",
               bot_owner_only=True)
    d.register("reject", cmd_reject_request, "拒绝好友/入群请求",
               bot_owner_only=True)
    # System (bot_owner only)
    d.register("enable", cmd_enable, "开启群聊机器人", bot_owner=True)
    d.register("disable", cmd_disable, "关闭群聊机器人", bot_owner=True)
    d.register("list", cmd_list, "查看群聊数据概览", bot_owner=True)
    # Title self-service (any member; silently ignored when bot is not group owner)
    d.register("mytitle", cmd_my_title, "我要头衔xxx 给自己设置专属头衔")
    # AI switches (bot owner / bot account only)
    d.register("私聊ai", cmd_private_ai_switch, "私聊AI开关 /私聊AI on/off/allow/deny",
               bot_owner=True)
    d.register("ai聊天", cmd_group_ai_switch, "本群AI聊天开关 /AI聊天 on/off",
               bot_owner=True)
    # uapis.cn fun commands (everyone)
    d.register("天气", cmd_weather, "真实天气 /天气 城市")
    d.register("热榜", cmd_hotboard, "热榜 /热榜 [平台]")
    d.register("热搜", cmd_hotboard, "热榜(别名)")
    d.register("一言", cmd_saying, "随机一言")
    d.register("答案之书", cmd_answerbook, "答案之书 /答案之书 [问题]")
    d.register("每日新闻", cmd_daily_news, "每日新闻图")
    d.register("必应壁纸", cmd_bing_wallpaper, "每日必应壁纸")
    d.register("epic免费", cmd_epic_free, "Epic免费游戏")
    d.register("二维码", cmd_qrcode, "生成二维码 /二维码 内容")
    d.register("节假日", cmd_holiday, "节假日与万年历 /节假日 [日期|月份|年份]")
    d.register("每日单词", cmd_daily_word, "每日单词 /每日单词 [词库] [数量]")
    d.register("github", cmd_github_lookup, "查询GitHub用户或仓库")
    d.register("网址状态", cmd_url_status, "检查公开网址状态")
    d.register("敏感词", cmd_sensitive_analyze, "分析敏感词 /敏感词 词1,词2")
    d.register("b站直播", cmd_bili_live, "查询B站直播间")
    d.register("b站用户", cmd_bili_user, "查询B站用户")
    d.register("b站评论", cmd_bili_replies, "查询B站评论")
    d.register("云ocr", cmd_cloud_ocr, "UApiS云端OCR /云OCR 图片URL")
    d.register("图片审核", cmd_nsfw_check, "图片NSFW安全检测")
    # admin commands
    d.register("全体", cmd_at_all, "@全体成员 /全体 内容",
               admin_only=True, bot_admin_required=True)
    d.register("acg图", cmd_acg_switch, "每日ACG图推送开关 /acg图 on/off",
               admin_only=True)
    d.register("热榜推送", cmd_hotboard_switch, "每日热榜推送开关 /热榜推送 on/off",
               admin_only=True)
    d.register("b站解析", cmd_bili_parse_switch, "B站视频自动解析开关 /b站解析 on/off",
               admin_only=True)
    d.register("gal", cmd_touchgal, "查询Galgame资源页 /gal 作品名")
    d.register("galgame", cmd_touchgal, "查询Galgame资源页 /galgame 作品名")
    d.register("游戏资源", cmd_touchgal, "查询Galgame资源页 /游戏资源 作品名")
    d.register("gal资源", cmd_touchgal_switch, "Galgame资源自动回复开关 /gal资源 on/off",
               admin_only=True)
    # master commands
    d.register("b站推送", cmd_bili_push, "盯UP主新投稿 /b站推送 add/del/list",
               bot_owner=True)
    d.register("积分", cmd_uapi_status, "查看uapis积分额度",
               bot_owner=True)
