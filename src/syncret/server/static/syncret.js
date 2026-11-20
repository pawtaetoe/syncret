// ask before a form marked data-confirm sends, the form still works without scripts
document.addEventListener("submit", (event) => {
  const message = event.target.dataset ? event.target.dataset.confirm : ""
  if (message && !window.confirm(message)) {
    event.preventDefault()
  }
})
