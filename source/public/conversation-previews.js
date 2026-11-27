/* Presentation hooks for the pinned Chainlit 2.9.6 native DOM.
   Workflow actions remain in the existing components and server handlers. */
(() => {
  let queued = false;
  const enhance = () => {
    queued = false;
    const header = document.querySelector('#header > div:first-child');
    if (header && !header.querySelector('.bone-brand')) {
      const brand = document.createElement('span');
      brand.className = 'bone-brand';
      brand.textContent = 'BoneMetGPT';
      header.appendChild(brand);
    }
    document.querySelectorAll('.step[data-step-type]').forEach((step) => {
      step.classList.add('bone-step');
      step.firstElementChild?.classList.add('bone-step-host');
      step.closest('div.mx-auto.w-full.p-4')?.classList.add('bone-chat-rail');
      const row = step.querySelector(':scope > div > [id^="step-"] > .ai-message');
      const body = row?.lastElementChild;
      if (body && body.tagName !== 'SPAN') {
        body.classList.add('bone-message-body');
        const engineering = !!body.querySelector('.bone-task, .bone-csv-picker');
        body.classList.toggle('is-engineering', engineering);
        row.classList.toggle('is-engineering', engineering);
        row.dataset.boneRole = 'assistant';
      }
    });
    const welcome = document.querySelector('#welcome-screen');
    welcome?.closest('div.mx-auto.w-full.p-4')?.classList.add('bone-chat-rail');
    if (welcome && !welcome.querySelector('.bone-chat-initializing')) {
      const status = document.createElement('p');
      status.className = 'bone-chat-initializing';
      status.setAttribute('role', 'status');
      status.textContent = 'Preparing your workspace…';
      welcome.prepend(status);
    }
    document.querySelectorAll('#message-composer').forEach((composer) => {
      const rail = composer.closest('div.mx-auto.w-full.p-4');
      rail?.classList.add('bone-chat-rail');
      if (!composer.closest('#welcome-screen')) rail?.classList.add('bone-composer-rail');
      if (composer.closest('#welcome-screen')) {
        composer.inert = true;
        composer.dataset.boneInitializing = 'true';
      } else if (composer.dataset.boneInitializing) {
        composer.inert = false;
        delete composer.dataset.boneInitializing;
      }
    });
    document.querySelectorAll('.ai-message img[alt^="Avatar for "]').forEach((image) => {
      image.parentElement.classList.add('bone-assistant-avatar');
    });
    document.querySelectorAll('.step[data-step-type="user_message"]').forEach((step) => {
      const bubble = step.querySelector('.bg-accent');
      const row = bubble?.parentElement;
      if (!row) return;
      bubble.classList.add('bone-user-bubble');
      row.classList.add('bone-user-row');
      row.dataset.boneRole = 'user';
      if (row.querySelector('.bone-user-avatar')) return;
      const avatar = document.createElement('img');
      avatar.src = '/public/user.png';
      avatar.alt = '';
      avatar.setAttribute('aria-hidden', 'true');
      avatar.className = 'bone-user-avatar';
      row.appendChild(avatar);
    });
    const modelToggle = document.querySelector('#chat-settings-header-button');
    const modelTitle = document.querySelector('#chat-settings-sidebar-title');
    const modelPanel = modelTitle?.closest('[data-panel], [role="dialog"]');
    if (modelToggle) {
      modelToggle.setAttribute('aria-label', 'Models');
      modelToggle.setAttribute('aria-expanded', String(!!modelPanel));
      if (!modelToggle.querySelector('.bone-model-toggle-label')) {
        const label = document.createElement('span');
        label.className = 'bone-model-toggle-label';
        label.textContent = 'Models';
        modelToggle.appendChild(label);
        modelToggle.addEventListener('click', (event) => {
          const close = document.querySelector('#chat-settings-sidebar-title > button');
          if (close) {
            // The native close action also discards an unapplied selection.
            event.preventDefault();
            event.stopImmediatePropagation();
            close.click();
          }
        }, true);
      }
    }
    if (modelTitle) {
      const label = modelTitle.querySelector('span') || modelTitle;
      if (label.textContent !== 'Chat model') label.textContent = 'Chat model';
      const close = modelTitle.querySelector('button');
      close?.setAttribute('aria-label', 'Collapse model sidebar');
    }
    const applyModel = document.querySelector('#confirm-sidebar');
    if (applyModel) {
      const label = applyModel.querySelector('span') || applyModel;
      if (label.textContent !== 'Apply model') label.textContent = 'Apply model';
      const footer = applyModel.parentElement;
      for (const button of [modelTitle?.querySelector('button'), ...footer.querySelectorAll('button')]) {
        if (!button || button.dataset.boneModelFocus) continue;
        button.dataset.boneModelFocus = 'true';
        button.addEventListener('click', () => requestAnimationFrame(() => {
          if (!document.querySelector('#chat-settings-sidebar-title')) modelToggle?.focus();
        }));
      }
    }
  };
  const schedule = () => {
    if (!queued) { queued = true; requestAnimationFrame(enhance); }
  };
  const start = () => {
    enhance();
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !event.defaultPrevented
          && !document.querySelector('[role="listbox"], [role="dialog"]')) {
        document.querySelector('#chat-settings-sidebar-title > button')?.click();
      }
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
