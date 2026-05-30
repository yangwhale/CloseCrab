// extract_adoc.js — scrape one adoc's rendered body via a same-origin iframe.
// Must run on a docs.dingtalk.com page (sets document.domain so the
// alidocs.dingtalk.com preview iframe is same-origin and readable).
//
// Direct navigation to alidocs.dingtalk.com returns /note/forbidden — the
// preview MUST be loaded as an iframe inside docs.dingtalk.com, NOT full-page.
//
// Fill DOCKEY / DENTRYKEY from the node (dentry/list gives docKey + dentryKey).
// Run via evaluate_script with filePath:/tmp/ant-adoc-raw.json, pull via ssh.
// Returns {title, html(.body-editor-content innerHTML), imgCount, imgs[]}.
async () => {
  const DOCKEY = "__DOCKEY__";        // e.g. 4j6OJ5jL7L3bgq3p
  const DENTRYKEY = "__DENTRYKEY__";  // e.g. 8kBmkAwaiB8PPbJm
  const WORKSPACE = "15eGe6boVpjOez3N";
  const previewUrl = `https://alidocs.dingtalk.com/note/preview?biz_ver=10&docId=${DOCKEY}`
    + `&docType=doc&dontjump=true&utm_scene=team_space&platform=pc&mainsiteOrigin=mainsite`
    + `&from=dingnote&workspaceId=${WORKSPACE}&docKey=${DOCKEY}&dentryKey=${DENTRYKEY}#preview=true`;
  document.getElementById('__extr')?.remove();
  const f = document.createElement('iframe');
  f.id='__extr'; f.style.cssText='position:fixed;left:-9999px;width:1200px;height:2400px;';
  f.src = previewUrl; document.body.appendChild(f);
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  let root=null, err='', tries=0;
  for (let i=0;i<40;i++){
    await sleep(500); tries=i+1;
    try{
      const doc=f.contentDocument;
      if(!doc){err='no contentDocument (cross-origin)';continue;}
      root=doc.querySelector('.body-editor-content');
      const txt=root ? (root.innerText||'').length : 0;
      if(root && txt>200){ await sleep(800); break; }  // settle lazy imgs/tables
    }catch(e){err='EXCEPTION: '+String(e).slice(0,100);}
  }
  if(!root) return {error: err || 'no .body-editor-content', tries};
  const doc=f.contentDocument;
  // title from the dedicated title area, then strip chrome on the Python side
  let title='';
  const ta=doc.querySelector('#doc-title-area textarea, #doc-title-area [contenteditable], #doc-title-area');
  if(ta) title=(ta.value||ta.innerText||'').trim().split('\n')[0];
  const imgs=[...root.querySelectorAll('img')].map(i=>i.src).filter(s=>s&&!s.startsWith('data:'));
  const res={title, html: root.innerHTML, imgCount: imgs.length, imgs, tries};
  f.remove();
  return res;
}
