(function () {
  "use strict";

  // Filters apply as soon as a dropdown or checkbox changes; text boxes on Enter.
  document.querySelectorAll("form[data-auto-submit]").forEach(function (form) {
    form.addEventListener("change", function (event) {
      var t = event.target;
      if (t.tagName === "SELECT" || t.type === "checkbox") form.requestSubmit();
    });
    // Leave empty fields out of the address, so links stay short and clean.
    form.addEventListener("submit", function () {
      Array.prototype.forEach.call(form.elements, function (el) {
        if (el.name && el.value === "" && el.type !== "checkbox") el.disabled = true;
      });
    });
  });
  // Coming back with the Back button restores the page as it was; re-enable the fields.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-auto-submit] [disabled]").forEach(function (el) { el.disabled = false; });
  });

  // Long comments show their first lines with a "Show more" button.
  function clampLongText() {
    document.querySelectorAll(".clamp").forEach(function (el) {
      if (el.dataset.clampReady) return;
      el.dataset.clampReady = "1";
      if (el.scrollHeight <= el.clientHeight + 2) {
        el.classList.remove("clamp");
        return;
      }
      var button = document.createElement("button");
      button.type = "button";
      button.className = "show-more";
      button.textContent = "Show more";
      button.setAttribute("aria-expanded", "false");
      button.addEventListener("click", function () {
        var open = el.classList.toggle("clamp") === false;
        button.textContent = open ? "Show less" : "Show more";
        button.setAttribute("aria-expanded", open ? "true" : "false");
      });
      el.insertAdjacentElement("afterend", button);
    });
  }
  clampLongText();

  // Live job banner: what the agent is collecting. On a post page
  // ([data-post]) it follows that post only; the page reloads once the
  // job finishes so the new comments appear.
  var banner = document.querySelector("[data-job-banner]");
  if (!banner) return;
  var text = banner.querySelector("[data-job-text]");
  var onlyPost = banner.getAttribute("data-post");
  var hadActive = banner.hasAttribute("data-active");
  var failures = 0;

  function describe(jobs, agentOnline) {
    var running = jobs.filter(function (j) { return j.status === "running"; });
    var waiting = jobs.length - running.length;
    if (onlyPost) {
      if (running.length) {
        var n = running[0].progress;
        return "Collecting this post now" + (n ? " — " + n + " comments so far" : "…");
      }
      return "This post is in the queue to be collected." +
        (agentOnline ? "" : " The admin laptop agent is offline; it will run when the agent is back.");
    }
    var parts = [];
    if (running.length) {
      var job = running[0];
      parts.push("Collecting “" + job.title + "”" + (job.progress ? " — " + job.progress + " comments so far" : ""));
    }
    if (waiting) parts.push(waiting + (waiting === 1 ? " post" : " posts") + " waiting");
    if (!running.length && waiting && !agentOnline) {
      parts.push("the admin laptop agent is offline; jobs will run when it is back");
    }
    return parts.join(" · ");
  }

  function poll() {
    fetch("/jobs/status.json", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.redirected || !r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (data) {
        failures = 0;
        var jobs = data.active.filter(function (j) { return !onlyPost || String(j.post_id) === onlyPost; });
        if (jobs.length) {
          hadActive = true;
          text.textContent = describe(jobs, data.agent_online);
          banner.hidden = false;
        } else {
          banner.hidden = true;
          if (hadActive) { window.location.reload(); return; }
        }
        setTimeout(poll, 5000);
      })
      .catch(function () {
        failures += 1;
        if (failures < 5) setTimeout(poll, 10000);
      });
  }

  poll();

  // Buttons that do something big ask first.
  document.querySelectorAll("[data-confirm]").forEach(function (button) {
    button.addEventListener("click", function (event) {
      if (!window.confirm(button.dataset.confirm)) event.preventDefault();
    });
  });
})();
