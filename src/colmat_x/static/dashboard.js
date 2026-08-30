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
