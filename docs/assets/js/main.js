// Language toggle
document.addEventListener("DOMContentLoaded", () => {
  const preferred = localStorage.getItem("aismixer:lang") || "en";
  const setLang = (lang) => {
    document.querySelectorAll("[data-lang]").forEach(el => {
      el.style.display = (el.getAttribute("data-lang") === lang) ? "" : "none";
    });
    document.getElementById("btn-en").classList.toggle("active", lang==="en");
    document.getElementById("btn-bg").classList.toggle("active", lang==="bg");
    localStorage.setItem("aismixer:lang", lang);
  };
  document.getElementById("btn-en").addEventListener("click", () => setLang("en"));
  document.getElementById("btn-bg").addEventListener("click", () => setLang("bg"));
  setLang(preferred);
});