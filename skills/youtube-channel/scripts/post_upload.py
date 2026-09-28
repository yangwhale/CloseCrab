# 上传后用 API 补：字幕、标签、封面、播放列表位置
import os, sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
V, srt, thumb, pos, tags = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5].split(",")
yt = build('youtube', 'v3', credentials=Credentials.from_authorized_user_file(os.path.expanduser(os.environ.get('YT_TOKEN','~/.config/youtube/token.json'))))
yt.captions().insert(part='snippet', body={'snippet': {'videoId': V, 'language': 'zh-Hans', 'name': '中文'}}, media_body=MediaFileUpload(srt, mimetype='application/octet-stream')).execute()
sn = yt.videos().list(part='snippet', id=V).execute()['items'][0]['snippet']
sn.update(tags=tags, categoryId='27', defaultLanguage='zh-CN', defaultAudioLanguage='zh-CN')
yt.videos().update(part='snippet', body={'id': V, 'snippet': sn}).execute()
yt.thumbnails().set(videoId=V, media_body=MediaFileUpload(thumb)).execute()
yt.playlistItems().insert(part='snippet', body={'snippet': {'playlistId': os.environ['YT_PLAYLIST'], 'resourceId': {'kind': 'youtube#video', 'videoId': V}}}).execute()  # 顺序最后统一排
print('post ok', V)
