// enumerate.js — recursive walk of the DingTalk space tree via dentry/list API.
// Paste the function body into chrome-devtools-mcp evaluate_script while a
// docs.dingtalk.com page is selected (login cookie + XSRF must be present).
// Save output with filePath: /tmp/ant-tpu-enum-fresh.json, then pull via
//   ssh glinux 'cat /tmp/ant-tpu-enum-fresh.json' > /tmp/ant-tpu-enum-fresh.json
// Returns {ok,total,byType,byExt,apiErr,nodes:[{name,type,ext,size,depth,path,
//   updated,contentUpdated,updator,uuid,docKey,dentryKey,url}]}.
async () => {
  const xsrf = decodeURIComponent((document.cookie.match(/XSRF-TOKEN=([^;]+)/)||[])[1] || '');
  if (!xsrf) return {error: 'NO_XSRF_COOKIE - login likely expired, re-login in gLinux Chrome'};
  const root = 'pGBa2Lm8aGLllAvacMD4Q7v9VgN7R35y';   // TPU项目资料库 root
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  async function list(uuid){
    const r = await fetch(`https://docs.dingtalk.com/box/api/v2/dentry/list?dentryUuid=${uuid}&orderType=SORT_KEY&sortType=desc&listDentrySource=2&pageSize=200`,
      {headers:{'accept':'application/json','x-xsrf-token':xsrf}, credentials:'include'});
    if (!r.ok) throw new Error('HTTP '+r.status);
    const j = await r.json();
    return (j.data && j.data.children) || [];
  }
  const nodes = []; let apiErr = null;
  async function walk(uuid, depth, pathArr){
    let kids; try { kids = await list(uuid); } catch(e){ apiErr = String(e); return; }
    for (const c of kids){
      nodes.push({
        name: c.name, type: c.dentryType, ext: c.extension || '',
        size: c.fileSize || 0, depth, path: [...pathArr, c.name].join(' / '),
        updated: c.updatedTime || 0, contentUpdated: c.contentUpdatedTime || 0,
        updator: (c.updator && (c.updator.name||c.updator.nick)) || '',
        uuid: c.dentryUuid, docKey: c.docKey || '', dentryKey: c.dentryKey || '',
        url: (c.url && c.url.pcChildAppUrl) || ''
      });
      if (c.dentryType === 'folder'){ await sleep(120); await walk(c.dentryUuid, depth+1, [...pathArr, c.name]); }
    }
  }
  await walk(root, 0, []);
  const byType={}, byExt={};
  for (const n of nodes){ byType[n.type]=(byType[n.type]||0)+1;
    if(n.type==='file'){const e=n.ext||'(none)';byExt[e]=(byExt[e]||0)+1;} }
  return {ok:true, total:nodes.length, byType, byExt, apiErr, nodes};
}
