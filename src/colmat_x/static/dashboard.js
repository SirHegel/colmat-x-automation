"use strict";

const numberFormatter = new Intl.NumberFormat("es-CO");

for (const input of document.querySelectorAll("[data-character-input]")) {
  const outputName = input.dataset.characterInput;
  const output = document.querySelector(`[data-character-output="${outputName}"]`);
  if (!output) {
    continue;
  }

  const refreshCount = () => {
    const maximum = input.maxLength > 0 ? input.maxLength : 0;
    output.textContent = `${numberFormatter.format(input.value.length)} / ${numberFormatter.format(maximum)}`;
  };

  input.addEventListener("input", refreshCount);
  refreshCount();
}

const verificationCode = document.querySelector("#code[inputmode='numeric']");
if (verificationCode) {
  verificationCode.addEventListener("input", () => {
    verificationCode.value = verificationCode.value.replace(/\D/g, "").slice(0, 8);
  });
}

// Motor de IA: el selector de modelo solo muestra los modelos del proveedor elegido.
// La lista cerrada viene del servidor; aquí no se inyecta ningún valor nuevo.
for (const providerSelect of document.querySelectorAll("[data-engine-provider]")) {
  const capability = providerSelect.dataset.engineProvider;
  const modelSelect = document.querySelector(`[data-engine-model="${capability}"]`);
  if (!modelSelect) {
    continue;
  }

  const refreshModels = () => {
    let firstVisible = null;
    let selectionVisible = false;
    for (const option of modelSelect.options) {
      const visible = option.dataset.provider === providerSelect.value;
      option.hidden = !visible;
      option.disabled = !visible;
      if (visible && firstVisible === null) {
        firstVisible = option;
      }
      if (visible && option.selected) {
        selectionVisible = true;
      }
    }
    if (!selectionVisible && firstVisible !== null) {
      firstVisible.selected = true;
    }
  };

  providerSelect.addEventListener("change", refreshModels);
  refreshModels();
}

for (const form of document.querySelectorAll("form")) {
  form.addEventListener("submit", (event) => {
    const confirmation = form.dataset.confirm;
    if (confirmation && !window.confirm(confirmation)) {
      event.preventDefault();
      return;
    }

    const submitButton = form.querySelector("button[type='submit']");
    if (submitButton) {
      submitButton.setAttribute("aria-busy", "true");
      submitButton.disabled = true;
    }
  });
}
