const welcome = document.querySelector("#welcome");
const chat = document.querySelector("#chat");
const messagesElement = document.querySelector("#messages");
const form = document.querySelector("#form");
const input = document.querySelector("#input");
const send = document.querySelector("#send");
const profileElement = document.querySelector("#profile");
const profileItems = document.querySelector("#profile-items");
const suggestionsElement = document.querySelector("#suggestions");
const welcomeForm = document.querySelector("#welcome-form");
const welcomeInput = document.querySelector("#welcome-input");
const rememberDirection = document.querySelector("#remember-direction");
const resetDirection = document.querySelector("#reset-direction");
const directionStorageKey = "jarvet.direction.v1";
let messages = [];
let profile = {};
let selectedOccupation = null;
let savedProviders = [];
let directionRevision = 0;
let activeChatController = null;

function loadRememberedDirection() {
  try {
    const remembered = JSON.parse(localStorage.getItem(directionStorageKey));
    if (!remembered || remembered.remember !== true) return;
    profile = remembered.profile || {};
    selectedOccupation = remembered.selectedOccupation || null;
    savedProviders = Array.isArray(remembered.savedProviders) ? remembered.savedProviders : [];
    rememberDirection.checked = true;
  } catch {
    localStorage.removeItem(directionStorageKey);
  }
}

function persistDirection() {
  if (!rememberDirection.checked) return;
  localStorage.setItem(directionStorageKey, JSON.stringify({
    remember: true,
    profile,
    selectedOccupation,
    savedProviders,
  }));
}

function clearRememberedDirection() {
  localStorage.removeItem(directionStorageKey);
}

function resetDirectionState() {
  directionRevision += 1;
  activeChatController?.abort();
  activeChatController = null;
  profile = {};
  selectedOccupation = null;
  savedProviders = [];
  rememberDirection.checked = false;
  clearRememberedDirection();
  renderProfile();
  for (const button of document.querySelectorAll(".save-provider")) {
    button.classList.remove("saved");
    button.setAttribute("aria-pressed", "false");
    button.title = "Save provider to memory";
    button.innerHTML = '<span aria-hidden="true">☆</span><span>Save</span>';
  }
  send.disabled = false;
}

const startingPoints = [
  {
    title: "Benefits",
    options: [
      "I was just discharged",
      "Help me use my GI Bill benefits",
      "I used all of my GI Bill benefits. What now?",
      "My benefits expired. What can I do?",
      "I don't qualify for benefits",
    ],
  },
  {
    title: "School and training",
    options: [
      "I can't find a school for the degree I want",
      "I want vocational training",
      "I want on-the-job training",
      "I want to get paid while I train",
    ],
  },
  {
    title: "Career ideas",
    options: [
      "I want an A+ computer technician certificate",
      "I want to be an underwater welder",
    ],
  },
];

const missionOpeners = {
  "Help me get started finding an education or career path.": {
    message: "Let's find your fastest path. First, do you currently have VA education benefits, such as the GI Bill or VR&E, that you may be eligible to use?",
    suggestions: [
      "Yes, I have GI Bill benefits",
      "I have a disability rating / VR&E",
      "I thought my GI Bill was gone",
      "Not sure what I have",
    ],
  },
  "Help me understand how I can get paid to go to school.": {
    message: "Here are the main ways school may be funded:\n\n• Post-9/11 GI Bill can pay tuition directly to the school and may include a monthly housing allowance and book stipend.\n• Yellow Ribbon can help with tuition above the GI Bill cap at participating schools.\n• VR&E may fund training and provide a subsistence allowance for eligible Veterans with a service-connected disability.\n• If your GI Bill eligibility period ended, there may be circumstances where you can request an extension. Eligibility depends on your situation, so we'll verify the official path before you act.",
    suggestions: [
      "How do I request an extension?",
      "Do I qualify for VR&E?",
      "I'm out of GI Bill months",
      "Help me understand FAFSA",
    ],
  },
  "I want to earn a paycheck while I train.": {
    message: "GI Bill On-the-Job Training and apprenticeships let you work in a real paid job while learning the role. Your employer pays wages, which generally increase as your skills progress, and eligible Veterans may also receive a monthly GI Bill payment that steps down as wages rise.\n\nTell me a location and the kind of work you want, and I'll look for relevant approved opportunities.",
    suggestions: [
      "On-the-job training in Texas",
      "Apprenticeships nationwide",
      "How much would I actually get paid?",
      "I want A+ certification",
    ],
  },
  "Help me figure out what to study.": {
    message: "We can work backward from a career goal, compare occupations using O*NET evidence, and then find matching degree or certificate programs by location. You do not need to know the school first.\n\nChoose an example below, or tell me where you live and what kind of work interests you.",
    suggestions: [
      "Marketing programs in San Jose",
      "Nursing programs in Oakland",
      "Welding certificates near me",
      "Help me explore careers first",
    ],
  },
  "I want an A+ computer technician certification.": {
    message: "CompTIA A+ can be a direct route into entry-level IT support work. GI Bill or VR&E may fund approved training, some employers offer IT support apprenticeships or OJT, and eligible licensing or certification test fees may be reimbursable separately from tuition. WIOA may also fund short credentials for eligible job seekers.\n\nTell me your location and we'll find the most relevant route.",
    suggestions: [
      "Find A+ training programs near me",
      "Find an IT apprenticeship instead",
      "I don't have GI Bill benefits left",
    ],
  },
  "Help me understand education benefits for my family.": {
    message: "Several programs may support a spouse or child:\n\n• Transfer of Post-9/11 GI Bill benefits generally must be requested while you are still serving and may carry a service obligation.\n• The Fry Scholarship may support eligible children or surviving spouses of a service member who died in the line of duty.\n• Chapter 35 may support eligible dependents of a Veteran who is permanently and totally disabled due to service, or who died from a service-connected cause.\n• Some states offer dependent tuition waivers with their own residency and eligibility rules.",
    suggestions: [
      "How do I transfer my GI Bill?",
      "Do I qualify for Chapter 35?",
      "Find state tuition waivers",
      "Tell me about the Fry Scholarship",
    ],
  },
};

function appendLinkedText(element, content, resources) {
  const links = resources.flatMap(resource =>
    (resource.inline_labels || []).map(label => ({
      label,
      url: resource.url,
      priority: resource.kind === "program-details" ? 3
        : resource.kind === "provider-details" ? 2 : 1,
    }))
  ).filter(link => link.label && link.url)
    .sort((first, second) => second.label.length - first.label.length
      || second.priority - first.priority);
  if (!links.length) {
    element.textContent = content;
    return;
  }

  const escaped = links.map(link => link.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(`(${escaped.join("|")})`, "gi");
  const byLabel = new Map();
  for (const link of links) {
    if (!byLabel.has(link.label.toLowerCase())) {
      byLabel.set(link.label.toLowerCase(), link);
    }
  }
  for (const part of content.split(pattern)) {
    const link = byLabel.get(part.toLowerCase());
    if (!link) {
      element.append(document.createTextNode(part));
      continue;
    }
    const anchor = document.createElement("a");
    anchor.href = link.url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    anchor.textContent = part;
    element.append(anchor);
  }
}

function addMessage(role, content, extraClass = "", resources = []) {
  const element = document.createElement("div");
  element.className = `message ${role} ${extraClass}`;
  appendLinkedText(element, content, resources);
  messagesElement.appendChild(element);
  messagesElement.scrollTop = messagesElement.scrollHeight;
  return element;
}

function renderSuggestions(suggestions = []) {
  suggestionsElement.replaceChildren();
  for (const suggestion of suggestions) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = suggestion.label;
    button.addEventListener("click", () => submitMessage(suggestion.value));
    suggestionsElement.appendChild(button);
  }
}

function renderStartingPoints() {
  const panel = document.createElement("section");
  panel.className = "starting-points";
  for (const group of startingPoints) {
    const section = document.createElement("div");
    section.className = "starting-group";
    const title = document.createElement("h2");
    title.textContent = group.title;
    section.appendChild(title);
    for (const option of group.options) {
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = `<span>${option}</span><span aria-hidden="true">→</span>`;
      button.addEventListener("click", () => submitMessage(option));
      section.appendChild(button);
    }
    panel.appendChild(section);
  }
  messagesElement.appendChild(panel);
}

function renderResources(resources = []) {
  if (!resources.length) return;
  const list = document.createElement("div");
  list.className = "resource-list";
  const groups = new Map();
  const standalone = [];
  for (const resource of resources) {
    if (!resource.group) {
      standalone.push(resource);
      continue;
    }
    if (!groups.has(resource.group)) groups.set(resource.group, []);
    groups.get(resource.group).push(resource);
  }

  const appendLink = (parent, resource) => {
    const link = document.createElement("a");
    link.href = resource.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = resource.label;
    link.innerHTML = `<span>${resource.action || resource.label}</span><span aria-hidden="true">↗</span>`;
    parent.appendChild(link);
  };

  const formatMoney = value => Number.isFinite(Number(value))
    ? new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value)
    : null;

  const providerIsSaved = provider => savedProviders.some(
    saved => saved.facility_code === provider.facility_code
  );

  const updateSaveButtons = () => {
    for (const button of document.querySelectorAll(".save-provider")) {
      const saved = savedProviders.some(provider => provider.facility_code === button.dataset.facilityCode);
      button.classList.toggle("saved", saved);
      button.setAttribute("aria-pressed", String(saved));
      button.title = saved ? "Remove from saved providers" : "Save provider to memory";
      button.innerHTML = `<span aria-hidden="true">${saved ? "★" : "☆"}</span><span>${saved ? "Saved" : "Save"}</span>`;
    }
  };

  const toggleProvider = provider => {
    if (providerIsSaved(provider)) {
      savedProviders = savedProviders.filter(saved => saved.facility_code !== provider.facility_code);
    } else {
      savedProviders.push({
        facility_code: provider.facility_code,
        institution: provider.institution,
        city: provider.city || "",
        state: provider.state || "",
        detail_url: provider.detail_url || "",
      });
      rememberDirection.checked = true;
    }
    persistDirection();
    renderProfile();
    updateSaveButtons();
  };

  const appendProviderDetails = (group, provider) => {
    const glance = document.createElement("p");
    glance.className = "provider-glance";
    const glanceParts = [];
    const glanceHousing = formatMoney(provider.estimated_housing_allowance);
    if (glanceHousing) glanceParts.push(`${glanceHousing}/mo housing`);
    if (provider.accredited) glanceParts.push("Accredited");
    if (provider.caution_flag) glanceParts.push("⚠ Caution flag");
    glance.textContent = glanceParts.join(" · ");
    if (glanceParts.length) group.appendChild(glance);

    const details = document.createElement("details");
    details.className = "provider-details";
    details.open = false;
    const summary = document.createElement("summary");
    summary.textContent = "Benefits, contact and approved programs";
    details.appendChild(summary);

    const facts = document.createElement("dl");
    facts.className = "provider-facts";
    const factValues = [
      ["Housing estimate", formatMoney(provider.estimated_housing_allowance), "/ month"],
      ["GI Bill students", provider.gi_bill_students, ""],
      ["In-state tuition", formatMoney(provider.tuition_in_state), "/ year"],
      ["Yellow Ribbon", provider.yellow_ribbon ? "Participates" : "Not listed", ""],
    ];
    for (const [label, value, suffix] of factValues) {
      if (value === null || value === undefined || value === "") continue;
      const wrapper = document.createElement("div");
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = `${value}${suffix}`;
      wrapper.append(term, description);
      facts.appendChild(wrapper);
    }
    details.appendChild(facts);

    if (provider.contact?.name) {
      const contact = document.createElement("p");
      contact.className = "provider-contact";
      const contactLabel = document.createElement("strong");
      const contactValue = document.createElement("span");
      contactLabel.textContent = "School certifying official";
      contactValue.textContent = `${provider.contact.name}${provider.contact.title ? ` · ${provider.contact.title}` : ""}`;
      contact.append(contactLabel, contactValue);
      details.appendChild(contact);
    }

    for (const summaryData of provider.program_summaries || []) {
      const programSection = document.createElement("section");
      programSection.className = "program-summary";
      const heading = document.createElement("h4");
      heading.textContent = `${summaryData.label} (${summaryData.total})`;
      programSection.appendChild(heading);
      const categoryCountLabel = (category, count) => {
        if (category === "Apprenticeship") {
          return `${count} apprenticeship${count === 1 ? "" : "s"}`;
        }
        if (category === "On-the-job training") {
          return `${count} OJT program${count === 1 ? "" : "s"}`;
        }
        return `${count} other approved program${count === 1 ? "" : "s"}`;
      };
      const categoryCounts = Object.entries(summaryData.category_counts || {})
        .filter(([, count]) => count > 0)
        .map(([category, count]) => categoryCountLabel(category, count));
      if (categoryCounts.length) {
        const breakdown = document.createElement("p");
        breakdown.className = "program-breakdown";
        breakdown.textContent = categoryCounts.join(" · ");
        programSection.appendChild(breakdown);
      }
      if (summaryData.programs?.length) {
        if (summaryData.selection === "sample") {
          const sampleNote = document.createElement("p");
          sampleNote.textContent = "No close title match; showing approved programs from this provider.";
          programSection.appendChild(sampleNote);
        }
        const programs = document.createElement("ul");
        for (const program of summaryData.programs) {
          const item = document.createElement("li");
          const programName = typeof program === "string" ? program : program.name;
          const programCategory = typeof program === "string" ? "" : program.category;
          item.textContent = programName;
          if (programCategory) {
            const category = document.createElement("span");
            category.className = "program-category";
            category.textContent = programCategory;
            item.appendChild(category);
          }
          programs.appendChild(item);
        }
        programSection.appendChild(programs);
        if (summaryData.matching > summaryData.programs.length) {
          const more = document.createElement("p");
          more.textContent = `${summaryData.matching - summaryData.programs.length} more relevant approved programs`;
          programSection.appendChild(more);
        }
      } else if (summaryData.total) {
        const empty = document.createElement("p");
        empty.textContent = "No close match to the current direction. Browse the full approved list.";
        programSection.appendChild(empty);
      }
      const typePaths = {
        IHL: "institution-of-higher-learning",
        NCD: "non-college-degree",
        OJT: "on-the-job-training-apprenticeship",
        APP: "on-the-job-training-apprenticeship",
      };
      const typePath = typePaths[summaryData.type];
      if (typePath && provider.detail_url) {
        const link = document.createElement("a");
        link.href = `${provider.detail_url}/${typePath}`;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Browse full VA list ↗";
        programSection.appendChild(link);
      }
      details.appendChild(programSection);
    }
    group.appendChild(details);
  };

  for (const [name, groupedResources] of groups) {
    const group = document.createElement("section");
    group.className = "resource-group";
    const provider = groupedResources.find(resource => resource.provider)?.provider;
    const heading = document.createElement("div");
    heading.className = "resource-heading";
    const title = document.createElement("h3");
    title.textContent = name;
    heading.appendChild(title);
    if (provider?.facility_code) {
      const saveButton = document.createElement("button");
      saveButton.type = "button";
      saveButton.className = "save-provider";
      saveButton.dataset.facilityCode = provider.facility_code;
      saveButton.addEventListener("click", () => toggleProvider(provider));
      heading.appendChild(saveButton);
    }
    group.appendChild(heading);
    const actions = document.createElement("div");
    actions.className = "resource-actions";
    for (const resource of groupedResources) appendLink(actions, resource);
    group.appendChild(actions);
    if (provider) appendProviderDetails(group, provider);
    list.appendChild(group);
  }

  if (standalone.length) {
    const official = document.createElement("section");
    official.className = "resource-group official-resources";
    const title = document.createElement("h3");
    title.textContent = groups.size ? "Official resources" : "Resources";
    official.appendChild(title);
    const actions = document.createElement("div");
    actions.className = "resource-actions";
    for (const resource of standalone) appendLink(actions, resource);
    official.appendChild(actions);
    list.appendChild(official);
  }
  messagesElement.appendChild(list);
  updateSaveButtons();
  messagesElement.scrollTop = messagesElement.scrollHeight;
}

function renderProfile() {
  profileItems.replaceChildren();
  if (selectedOccupation) {
    const occupation = document.createElement("button");
    occupation.type = "button";
    occupation.className = "profile-item";
    occupation.title = `Stop focusing on ${selectedOccupation.title}`;
    occupation.textContent = selectedOccupation.title;
    occupation.addEventListener("click", () => {
      selectedOccupation = null;
      renderProfile();
      persistDirection();
    });
    profileItems.appendChild(occupation);
  }
  for (const provider of savedProviders) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "profile-item saved-provider-item";
    item.title = `Remove ${provider.institution} from saved providers`;
    item.textContent = `Saved: ${provider.institution}`;
    item.addEventListener("click", () => {
      savedProviders = savedProviders.filter(saved => saved.facility_code !== provider.facility_code);
      renderProfile();
      persistDirection();
      for (const button of document.querySelectorAll(`.save-provider[data-facility-code="${provider.facility_code}"]`)) {
        button.classList.remove("saved");
        button.setAttribute("aria-pressed", "false");
        button.innerHTML = '<span aria-hidden="true">☆</span><span>Save</span>';
      }
    });
    profileItems.appendChild(item);
  }
  for (const [field, values] of Object.entries(profile)) {
    for (const value of values) {
      if (selectedOccupation && value.toLowerCase() === selectedOccupation.title.toLowerCase()) {
        continue;
      }
      const item = document.createElement("button");
      item.type = "button";
      item.className = "profile-item";
      item.title = `Remove ${value}`;
      item.textContent = value;
      item.addEventListener("click", () => {
        profile[field] = profile[field].filter(entry => entry !== value);
        renderProfile();
        persistDirection();
      });
      profileItems.appendChild(item);
    }
  }
}

function begin(options = {}) {
  welcome.hidden = true;
  chat.hidden = false;
  messages = [];
  messagesElement.replaceChildren();
  renderProfile();
  addMessage("assistant", options.message || "What do you need help with today? Choose a starting point or describe your situation in your own words.");
  if (options.message) {
    messages.push({ role: "assistant", content: options.message });
  } else {
    renderStartingPoints();
  }
  renderSuggestions((options.suggestions || []).map(label => ({ label, value: label })));
  input.focus();
}

function showHome(event) {
  event?.preventDefault();
  chat.hidden = true;
  welcome.hidden = false;
  messages = [];
  messagesElement.replaceChildren();
  suggestionsElement.replaceChildren();
  welcomeInput.value = "";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function beginWithMission(content) {
  const opener = missionOpeners[content];
  if (!opener) {
    begin();
    submitMessage(content);
    return;
  }
  begin(opener);
}

welcomeForm.addEventListener("submit", event => {
  event.preventDefault();
  const content = welcomeInput.value.trim();
  if (content) {
    begin();
    submitMessage(content);
  }
});
for (const mission of document.querySelectorAll(".mission")) {
  mission.addEventListener("click", () => beginWithMission(mission.dataset.message));
}
document.querySelector("#chat-home").addEventListener("click", showHome);
document.querySelector("#rewind-home").addEventListener("click", showHome);
rememberDirection.addEventListener("change", () => {
  if (rememberDirection.checked) persistDirection();
  else clearRememberedDirection();
});
resetDirection.addEventListener("click", () => {
  resetDirectionState();
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
});
input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

const thinkingStatuses = [
  "Finding the right path…",
  "Checking O*NET occupation data…",
  "Looking up approved providers…",
  "Verifying program pages…",
  "Comparing benefit details…",
  "Reviewing official sources…",
  "Still working — double-checking results…",
];

function startThinkingStatuses(element) {
  let index = 0;
  element.textContent = thinkingStatuses[0];
  const timer = setInterval(() => {
    index = Math.min(index + 1, thinkingStatuses.length - 1);
    element.textContent = thinkingStatuses[index];
  }, 3000);
  return () => clearInterval(timer);
}

async function submitMessage(rawContent) {
  const content = rawContent.trim();
  if (!content) return;
  document.querySelector(".starting-points")?.remove();
  renderSuggestions();
  messages.push({ role: "user", content });
  addMessage("user", content);
  input.value = "";
  input.style.height = "auto";
  send.disabled = true;
  const thinking = addMessage("assistant", "Finding the right path…", "thinking");
  const stopThinkingStatuses = startThinkingStatuses(thinking);
  const requestRevision = directionRevision;
  const controller = new AbortController();
  activeChatController = controller;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        messages,
        profile,
        selected_occupation: selectedOccupation,
        saved_providers: savedProviders,
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Request failed");
    if (requestRevision !== directionRevision) {
      stopThinkingStatuses();
      thinking.remove();
      return;
    }
    stopThinkingStatuses();
    thinking.remove();
    messages.push({ role: "assistant", content: body.message });
    addMessage("assistant", body.message, "", body.resources || []);
    renderResources(body.resources);
    profile = body.profile || profile;
    selectedOccupation = body.selected_occupation || selectedOccupation;
    renderProfile();
    persistDirection();
    renderSuggestions(body.suggestions);
  } catch (error) {
    stopThinkingStatuses();
    if (error.name === "AbortError") {
      thinking.remove();
      return;
    }
    thinking.textContent = `I couldn't reach the guide: ${error.message}`;
    thinking.classList.remove("thinking");
  } finally {
    if (activeChatController === controller) {
      activeChatController = null;
      send.disabled = false;
    }
    input.focus();
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  await submitMessage(input.value);
});

loadRememberedDirection();