document.addEventListener('DOMContentLoaded', () => {
  const $html = document.documentElement;
  const btnEn = document.getElementById('btn-en');
  const btnBg = document.getElementById('btn-bg');
  function setLang(lang, {remember=true, updateHash=true} = {}) {
    $html.setAttribute('data-lang', lang);
    btnEn?.classList.toggle('active', lang === 'en');
    btnBg?.classList.toggle('active', lang === 'bg');
    const ogLocale = document.querySelector('meta[property="og:locale"]');
    if (ogLocale) ogLocale.setAttribute('content', lang === 'bg' ? 'bg_BG' : 'en_US');
    if (remember) localStorage.setItem('aismixer_lang', lang);
    if (updateHash) {
      if (lang === 'bg') history.replaceState(null, '', '#bg');
      else history.replaceState(null, '', location.pathname + location.search);
    }
  }
  let start = (location.hash === '#bg' || location.search.includes('lang=bg')) ? 'bg'
             : (localStorage.getItem('aismixer_lang') || 'en');
  setLang(start, {updateHash:false});
  btnEn?.addEventListener('click', () => setLang('en'));
  btnBg?.addEventListener('click', () => setLang('bg'));
});
