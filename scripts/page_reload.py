"""The reload check every served page carries (injected by pages.py at serve time, not written into the file).

It polls the file's Last-Modified with a HEAD every 5 s and when the tab is shown again (a hidden tab's timers are
throttled). A change reloads the page, except while the user has unsaved input in a form (typing, a picked option): then it shows one notice with a button.
"""

# Set by the timer's reload, so the page server does not count it as a person looking at the board (#229); the
# notice's Reload button is the person's own click and sets none.
RELOAD_COOKIE = "cos-reload=1"

SNIPPET = """<script>
(function(){
  var seen=Date.parse(document.lastModified);
  function some(sel,f){return Array.prototype.some.call(document.querySelectorAll(sel),f);}
  function typing(){var a=document.activeElement;
    return a&&(/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)||a.isContentEditable)||
      some('form input,form textarea',function(e){return e.value!==e.defaultValue||e.checked!==e.defaultChecked;})||
      some('form option',function(o){return o.selected!==o.defaultSelected;});}
  function notice(){
    if(document.getElementById('page-changed'))return;
    var n=document.createElement('div');n.id='page-changed';n.setAttribute('role','status');
    n.style.cssText='position:fixed;right:16px;bottom:16px;z-index:99999;padding:8px 12px;background:#222;color:#fff;font:14px/1.4 system-ui,sans-serif;border-radius:6px';
    n.textContent='This page changed. ';
    var b=document.createElement('button');b.textContent='Reload';b.onclick=function(){location.reload();};
    n.appendChild(b);document.body.appendChild(n);
  }
  function poll(){
    fetch(location.pathname,{method:'HEAD',cache:'no-store'}).then(function(r){
      var t=Date.parse(r.headers.get('Last-Modified'));
      if(!isNaN(t)&&t!==seen){if(typing())notice();else{document.cookie='%s;max-age=10;path=/';location.reload();}}
    }).catch(function(){});
  }
  setInterval(poll,5000);document.addEventListener('visibilitychange',poll);
})();
</script>
""" % RELOAD_COOKIE


ENCODED = SNIPPET.encode()


def inject(page: bytes) -> bytes:
    """`page` with SNIPPET before its last `</body>`, or appended when it has none."""
    at = page.lower().rfind(b"</body>")
    return page + ENCODED if at < 0 else page[:at] + ENCODED + page[at:]
