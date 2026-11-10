// Live preview of a pasted Workshop link: thumbnail, title, game and whether
// it can be downloaded. Uses jQuery. Everything from Steam is inserted as
// text, never as HTML.
$(function () {
  const $url = $("#workshopUrl");
  const $preview = $("#itemPreview");
  const $game = $("#gameSelect");
  const $submit = $("#jobForm .submit-button");
  let timer = null;
  let request = null;
  let lastId = null;
  let lastResult = null;

  function itemId(value) {
    const text = value.trim();
    if (/^\d{5,20}$/.test(text)) return text;
    if (!/steamcommunity\.com\//i.test(text)) return null;
    const match = text.match(/[?&]id=(\d{5,20})/);
    return match ? match[1] : null;
  }

  function token() {
    return ($("#adminToken").val() || "").trim();
  }

  function compactNumber(value) {
    return new Intl.NumberFormat("en", { notation: "compact" }).format(value);
  }

  function sizeText(bytes) {
    if (!bytes) return "";
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  }

  function show($content, state) {
    $preview.stop(true, true)
      .attr("class", `item-preview ${state}`)
      .empty()
      .append($content);
    if ($preview.prop("hidden")) {
      $preview.prop("hidden", false).hide().slideDown(160);
    }
  }

  function hide() {
    lastId = null;
    lastResult = null;
    updateSubmit();
    if (!$preview.prop("hidden")) {
      $preview.stop(true, true).slideUp(140, () => $preview.prop("hidden", true).empty());
    }
  }

  function showLoading() {
    show(
      $("<div class='preview-skeleton'>").append(
        $("<div class='shimmer thumb'>"),
        $("<div class='lines'>").append(
          $("<div class='shimmer line wide'>"),
          $("<div class='shimmer line'>"),
          $("<div class='shimmer line short'>"),
        ),
      ),
      "loading",
    );
  }

  function showMessage(text, state) {
    show($("<div class='preview-message'>").text(text), state);
  }

  function render(data) {
    lastResult = data;
    if (!data.found) {
      showMessage(data.error || "Steam does not know this item.", "bad");
      updateSubmit();
      return;
    }
    const state = !data.supported ? "bad" : data.accounts ? "good" : "warn";
    const $thumb = $("<div class='preview-thumb'>");
    if (data.preview_url) {
      $("<img alt='' loading='lazy' referrerpolicy='no-referrer'>")
        .attr("src", data.preview_url)
        .on("error", function () { $(this).remove(); })
        .appendTo($thumb);
    }
    const $title = $("<a class='preview-title' target='_blank' rel='noopener noreferrer'>")
      .attr("href", `https://steamcommunity.com/sharedfiles/filedetails/?id=${data.workshop_id}`)
      .text(data.title);
    const $meta = $("<div class='preview-meta'>").append(
      $("<span class='preview-game'>").text(data.game),
    );
    if (data.subscriptions) {
      $meta.append($("<span>").text(`${compactNumber(data.subscriptions)} subscribers`));
    }
    if (data.file_size) $meta.append($("<span>").text(sizeText(data.file_size)));
    if (data.time_updated) {
      $meta.append($("<span>").text(
        `updated ${new Date(data.time_updated * 1000).toLocaleDateString()}`,
      ));
    }
    let status;
    if (!data.supported) {
      status = `${data.game} is not supported yet. Share an account that owns it to add it.`;
    } else if (!data.accounts) {
      status = `No working account owns ${data.game} right now. Try again later.`;
    } else {
      status = `Ready to download · ${data.accounts} account${data.accounts === 1 ? "" : "s"}`;
    }
    const $status = $("<div class='preview-status'>").text(status);
    if (!data.supported) {
      $status.append(" ", $("<a href='#share'>").text("Share an account"));
    }
    show(
      $("<div class='preview-card'>").append(
        $thumb,
        $("<div class='preview-body'>").append($title, $meta, $status),
      ),
      state,
    );
    if ($game.val() !== "" && $game.val() !== String(data.app_id)) {
      $status.after($("<div class='preview-warning'>").text(
        `You picked another game. Set the game to "Detect from link" to use ${data.game}.`,
      ));
    }
    updateSubmit();
  }

  function updateSubmit() {
    const detecting = $game.val() === "";
    const blocked = detecting && lastResult && (!lastResult.found || !lastResult.supported || !lastResult.accounts);
    $submit.prop("disabled", Boolean(blocked));
    $submit.text(
      lastResult && lastResult.found && lastResult.supported && detecting
        ? `Add ${lastResult.game} item to queue`
        : "Add to queue",
    );
  }

  function lookup() {
    const value = $url.val();
    const id = itemId(value);
    if (!id) {
      if (value.trim().length > 8) {
        lastId = null;
        lastResult = null;
        showMessage("Paste a steamcommunity.com Workshop link or an item ID.", "hint");
        updateSubmit();
      } else {
        hide();
      }
      return;
    }
    if (id === lastId) return;
    if (!token()) {
      showMessage("Enter the site password first to look up the item.", "hint");
      return;
    }
    lastId = id;
    lastResult = null;
    showLoading();
    if (request) request.abort();
    request = $.ajax({
      url: "/api/workshop/lookup",
      data: { url: value.trim() },
      headers: { Authorization: `Bearer ${token()}` },
      dataType: "json",
      cache: false,
    })
      .done(render)
      .fail((xhr, status) => {
        if (status === "abort") return;
        lastId = null;
        const error = xhr.responseJSON && xhr.responseJSON.error;
        showMessage(error || "Could not look up this item. Try again.", "bad");
      });
  }

  $url.on("input paste", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(lookup, 200);
  });
  $game.on("change", updateSubmit);
  $("#adminToken").on("change", () => { lastId = null; lookup(); });

  // app.js clears the field after a successful request.
  $("#jobForm").on("workshop:queued", hide);
});
