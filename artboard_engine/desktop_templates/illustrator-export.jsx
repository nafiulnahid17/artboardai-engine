// Run ONLY inside licensed Adobe Illustrator with the matching EPS active.
#target illustrator
(function () {
    var request = __REQUEST__;
    var root = File($.fileName).parent.parent;
    var expected = File(root.fsName + "/" + request.side + "-body.eps");
    var destination = File(root.fsName + "/" + request.side + "-body.ai");
    function check(doc) {
        if (doc.artboards.length !== 1) throw new Error("Expected one artboard.");
        var r = doc.artboards[0].artboardRect;
        if (Math.abs(r[2] - r[0] - 1584) > 0.05 || Math.abs(r[1] - r[3] - 2232) > 0.05)
            throw new Error("Canvas must be exactly 22 x 31 inches.");
        if (doc.documentColorSpace !== DocumentColorSpace.CMYK) throw new Error("Document must be CMYK.");
        if (!doc.pathItems.length || doc.rasterItems.length || doc.placedItems.length || doc.textFrames.length)
            throw new Error("Need real paths, no bitmaps, links or live fonts.");
        var nonCMYK = 0;
        for (var i = 0; i < doc.pathItems.length; i++) {
            var p = doc.pathItems[i];
            if ((p.filled && p.fillColor.typename !== "CMYKColor") ||
                (p.stroked && p.strokeColor.typename !== "CMYKColor")) nonCMYK++;
        }
        if (nonCMYK) throw new Error("Non-CMYK path colors found. Do not silently convert through a different profile.");
        return {
            width_points: r[2] - r[0], height_points: r[1] - r[3],
            color_mode: "CMYK", path_items: doc.pathItems.length,
            raster_items: doc.rasterItems.length, placed_items: doc.placedItems.length,
            live_text_items: doc.textFrames.length, non_cmyk_path_items: nonCMYK
        };
    }
    // ExtendScript ES3 does not consistently provide JSON.stringify.
    function stringify(value) {
        if (value === null) return "null";
        if (typeof value === "string")
            return '"' + value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\r/g, "\\r").replace(/\n/g, "\\n").replace(/\t/g, "\\t") + '"';
        if (typeof value === "number" || typeof value === "boolean") return String(value);
        var parts = [], k;
        for (k in value) if (value.hasOwnProperty(k)) parts.push(stringify(k) + ":" + stringify(value[k]));
        return "{" + parts.join(",") + "}";
    }
    try {
        if (!app.documents.length) throw new Error("Open the matching EPS first. Assign an ICC only if one was supplied.");
        var doc = app.activeDocument;
        if (doc.fullName.fsName.toLowerCase() !== expected.fsName.toLowerCase())
            throw new Error("Active document must be the original " + request.side + "-body.eps from this bundle.");
        if (destination.exists) throw new Error("AI already exists. It will not be overwritten.");
        check(doc);
        var profileConfirmed = false;
        if (request.profile_sha256 && !confirm("Have you assigned the bundled printer ICC profile through Edit > Assign Profile, preserving existing CMYK values?\n\n" +
            request.profile_description + "\nSHA-256: " + request.profile_sha256 +
            "\n\nThis is your confirmation, not an automatic profile-hash measurement."))
            throw new Error("Printer profile assignment not confirmed.");
        if (request.profile_sha256) profileConfirmed = true;
        if (!request.profile_sha256 && !confirm("No optional ICC was supplied. Export native AI with existing CMYK numbers, without claiming printer-specific color accuracy?"))
            throw new Error("Unmanaged CMYK export not confirmed.");
        var options = new IllustratorSaveOptions();
        options.compatibility = Compatibility.ILLUSTRATOR16;
        options.pdfCompatible = true;
        options.compressed = true;
        options.embedICCProfile = Boolean(request.profile_sha256);
        doc.saveAs(destination, options);
        doc.close(SaveOptions.DONOTSAVECHANGES);
        var reopened = app.open(destination);
        var report = check(reopened);
        report.bundle_id = request.bundle_id;
        report.side = request.side;
        report.source_eps_sha256 = request.eps_sha256[request.side];
        report.profile_sha256 = request.profile_sha256;
        report.application = "Illustrator";
        report.version = String(app.version);
        report.saved_using = "IllustratorSaveOptions";
        report.reopened = true;
        report.profile_assignment_operator_confirmed = profileConfirmed;
        report.file_bytes = destination.length;
        var receipt = File(root.fsName + "/desktop/" + request.side + "-illustrator-receipt.json");
        receipt.encoding = "UTF-8";
        if (!receipt.open("w")) throw new Error("Could not open receipt file for writing.");
        receipt.write(stringify(report));
        receipt.close();
        alert("Native AI saved and reopened successfully. Return the AI and receipt to the engine. This is artwork export, not production approval.");
    } catch (error) {
        alert("Artboard Ai export stopped: " + error.message + "\nAny partial file is unapproved. Do not replace existing evidence; use a new job.");
    }
})();
