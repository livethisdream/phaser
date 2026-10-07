// Low-assembly rotating stand for the CN0566 Phaser.
//
// Same geometry goals as ../stand (array phase center on the axis, 5-degree
// detents, engraved scale, GT2 rim for a future motor), but no bearings,
// BBs, plunger or ballast. Plastic-on-plastic plain bearing, a printed
// flexure detent, and six screws total.
//
// Frame: Z up, rotation axis = Z, +Y = boresight (patch face looks toward +Y
// when the pointer reads 0).
//
//   openscad -D 'part="base"' -o stl/base.stl phaser_stand_simple.scad
// Parts: base, platter, retainer, mast, lock_wheel, assembly (preview).

part = "assembly";

// ---------------------------------------------------------------- measured
board_w          = 113.19;
thread_from_left = 52.28;  // patch face toward you, bottom edge down
// VERIFY: assumed centered on the PCB. Measure left edge -> center of the
// leftmost patch column and add 49.0 (3.5 x 14 mm pitch).
array_center_from_left = board_w / 2;
thread_behind_board = 6.0;
board_t             = 1.6;

mast_x = array_center_from_left - thread_from_left;
mast_y = -(thread_behind_board + board_t / 2);

// ---------------------------------------------------------------- fits
fit   = 0.3;   // radial clearance on sliding fits (PLA/PETG)
m5_pilot = 4.2;  // M5 self-tapping into plastic
m4_pilot = 3.3;
qtr_head_af = 11.3;  // 1/4-20 hex head, 7/16" AF + clearance
qtr_head_h  = 4.6;
qtr_clear   = 6.8;
qtr_nut_af  = 11.3;
qtr_nut_h   = 5.8;
insert_d    = 5.6;   // optional M4 heat-set inserts for a motor bracket
insert_h    = 6;

// ---------------------------------------------------------------- geometry
base_d    = 240;
base_h    = 22;
track_r   = [84, 92];  // raised plain-bearing ring the platter rides on
track_h   = 1.2;
recess_d  = 44;        // underside pocket for the retainer
recess_h  = 10;

post_d    = 24;        // platter spindle, passes through the base

gt2_teeth = 300;       // 300T rim; 15:1 with a 20T pulley
platter_r = gt2_teeth * 2 / (2 * PI) - 0.254;
platter_h = 12;
belt_band = 9;

// Detent: a skirt under the platter with 72 vertical grooves on its inner
// face, and a vertical leaf spring in the base whose ridge clicks into them.
detent_step = 5;
skirt_r     = [56, 60];
skirt_h     = 8;
groove_d    = 1.2;
leaf_t      = 1.6;
leaf_w      = 14;
leaf_preload = 0.8;    // ridge interference with the skirt face

retainer_d = 36;
retainer_t = 4;

mast_foot_d = 64;
mast_foot_t = 6;
mast_pocket = 3;
mast_h      = 80;
mast_top    = 22;
mast_skin   = 4;

scale_r0 = platter_r + 2;
engrave  = 0.6;

platter_z = base_h + track_h;  // platter underside in the assembly

$fn = 96;
eps = 0.01;

module hex(af, h) { cylinder(d = af / cos(30), h = h, $fn = 6); }
module ring(r, h) { difference() { cylinder(r = r[1], h = h); translate([0, 0, -eps]) cylinder(r = r[0], h = h + 2 * eps); } }

// ---------------------------------------------------------------- base
module scale_marks() {
  for (a = [0 : 1 : 359]) if (abs(a - 180) > 10) {
    len = (a % 10 == 0) ? 6 : (a % 5 == 0) ? 4 : 2.2;
    w   = (a % 5 == 0) ? 0.8 : 0.5;
    rotate(-a) translate([-w / 2, scale_r0, base_h - engrave]) cube([w, len, engrave + eps]);
  }
  // 0 at boresight, positive clockwise seen from above.
  for (a = [-150 : 30 : 150])
    rotate(-a) translate([0, scale_r0 + 8.5, base_h - engrave]) linear_extrude(engrave + eps)
      text(str(a), size = 4.2, halign = "center", valign = "bottom", font = "DejaVu Sans:style=Bold");
}

motor_insert_r = scale_r0 + 6;
motor_insert_a = [180 - 7, 180 + 7];
leaf_a = 90;  // detent at +X
pocket_depth = skirt_h + 4;

module leaf() {
  // Vertical cantilever: anchored to the pocket floor, flexes radially.
  r_out = skirt_r[0] - fit - 0.4;
  floor_z = base_h - pocket_depth;
  rotate(-leaf_a) {
    translate([-leaf_w / 2, r_out - leaf_t, floor_z - eps])
      cube([leaf_w, leaf_t, pocket_depth - 0.6]);
    // Ridge: a vertical 90-degree V that reaches into the skirt grooves.
    reach = skirt_r[0] + leaf_preload - (r_out - leaf_t);
    translate([0, r_out - leaf_t, base_h - skirt_h + 0.5])
      linear_extrude(skirt_h - 2)
        polygon([[-reach, 0], [0, reach], [reach, 0]]);
  }
}

module base() {
  difference() {
    union() {
      cylinder(d = base_d, h = base_h);
      translate([0, 0, base_h - eps]) ring(track_r, track_h + eps);
    }
    // Spindle bore and retainer recess.
    translate([0, 0, -eps]) cylinder(d = post_d + 2 * fit, h = base_h + 1);
    translate([0, 0, -eps]) cylinder(d = recess_d, h = recess_h + eps);
    // Channel the platter skirt turns in.
    translate([0, 0, base_h - pocket_depth]) ring([skirt_r[0] - 4, skirt_r[1] + fit + 0.5], pocket_depth + eps);
    // Feet recesses (optional 12 mm stick-on feet).
    for (a = [45 : 90 : 359]) rotate(a) translate([0, base_d / 2 - 18, -eps]) cylinder(d = 12.5, h = 0.8 + eps);
    for (a = motor_insert_a) rotate(-a) translate([0, motor_insert_r, base_h - insert_h])
      cylinder(d = insert_d, h = insert_h + eps, $fn = 24);
    scale_marks();
  }
  leaf();
}

// ---------------------------------------------------------------- platter
module gt2_rim_2d() {
  difference() {
    circle(r = platter_r, $fn = gt2_teeth * 2);
    for (i = [0 : gt2_teeth - 1]) rotate(i * 360 / gt2_teeth)
      translate([platter_r + 0.2, 0]) circle(d = 1.3, $fn = 10);
  }
}

mast_screw_r = 26;
mast_screw_a = [90, 210, 330];

module mast_foot_2d(d) {
  intersection() { circle(d = d); translate([-d, -d / 2 + 4]) square([2 * d, 2 * d]); }
}

module platter() {
  post_len = platter_z - (recess_h - fit);  // ends just above the retainer
  difference() {
    union() {
      linear_extrude(belt_band) gt2_rim_2d();
      translate([0, 0, belt_band]) cylinder(r = platter_r, h = platter_h - belt_band, $fn = 240);
      translate([0, 0, -post_len]) cylinder(d = post_d, h = post_len + eps);
      // Detent skirt.
      translate([0, 0, -skirt_h]) ring(skirt_r, skirt_h + eps);
      // Pointer.
      translate([0, 0, platter_h - 3]) linear_extrude(3)
        polygon([[-5, platter_r - 6], [5, platter_r - 6], [0.6, scale_r0 + 5.5], [-0.6, scale_r0 + 5.5]]);
    }
    // Pilot for the retainer screw.
    translate([0, 0, -post_len - eps]) cylinder(d = m5_pilot, h = 16, $fn = 24);
    // 72 vertical V-grooves on the skirt's inner face.
    for (a = [0 : detent_step : 359]) rotate(a) translate([0, skirt_r[0], -skirt_h - eps])
      rotate(45) translate([-groove_d * 0.71, -groove_d * 0.71, 0])
        cube([groove_d * 1.42, groove_d * 1.42, skirt_h + 2 * eps]);
    // Mast pocket and screw pilots.
    translate([0, 0, platter_h - mast_pocket]) linear_extrude(mast_pocket + eps) offset(0.2) mast_foot_2d(mast_foot_d);
    for (a = mast_screw_a) rotate(a) translate([mast_screw_r, 0, platter_h - mast_pocket - 8])
      cylinder(d = m4_pilot, h = 8 + eps, $fn = 24);
    // Belt anchor slot at the rear.
    translate([-1.5, -platter_r - eps, -eps]) cube([3, 10, belt_band + eps]);
    // Lightening pockets.
    for (a = [30 : 60 : 359]) rotate(a) translate([0, 72, platter_h - 6]) cylinder(d = 18, h = 6 + eps);
  }
}

// Washer under the base; one M5 x 16 flat-head screw clamps it to the post.
module retainer() {
  difference() {
    cylinder(d = retainer_d, h = retainer_t);
    translate([0, 0, -eps]) cylinder(d = 5.4, h = retainer_t + 2 * eps, $fn = 24);
    translate([0, 0, -eps]) cylinder(d1 = 10.4, d2 = 5.4, h = 2.6 + eps, $fn = 32);
  }
}

// ---------------------------------------------------------------- mast
module mast() {
  top_z = mast_foot_t + mast_h - mast_pocket;
  difference() {
    union() {
      linear_extrude(mast_foot_t) mast_foot_2d(mast_foot_d);
      hull() {
        translate([mast_x, mast_y, mast_foot_t - eps]) linear_extrude(eps) square(mast_top + 10, center = true);
        translate([mast_x, mast_y, mast_foot_t + 20]) linear_extrude(eps) square(mast_top, center = true);
      }
      translate([mast_x, mast_y, mast_foot_t]) linear_extrude(top_z - mast_foot_t)
        offset(r = 2) square(mast_top - 4, center = true);
    }
    for (a = mast_screw_a) rotate(a) translate([mast_screw_r, 0, -eps]) {
      cylinder(d = 4.4, h = mast_foot_t + 2 * eps, $fn = 24);
      translate([0, 0, mast_foot_t - 3]) cylinder(d = 8, h = 60, $fn = 32);
    }
    translate([mast_x, mast_y, 0]) {
      // Stud: 1/4-20 x 3/4" hex bolt, head slid in from the rear. Mates with
      // the female thread in the Phaser block.
      translate([0, 0, top_z - mast_skin - eps]) cylinder(d = qtr_clear, h = mast_skin + 2 * eps, $fn = 32);
      translate([0, 0, top_z - mast_skin - qtr_head_h]) {
        hex(qtr_head_af, qtr_head_h);
        translate([-qtr_head_af / 2, -mast_top, 0]) cube([qtr_head_af, mast_top, qtr_head_h]);
      }
    }
    translate([mast_x - mast_top / 2, mast_y + mast_top / 2 - 2.5, top_z - 0.5]) cube([mast_top, 0.6, 0.5 + eps]);
  }
}

module lock_wheel() {
  d = 26; h = 8;
  difference() {
    cylinder(d = d, h = h);
    for (i = [0 : 29]) rotate(i * 12) translate([d / 2 + 0.3, 0, -eps]) cylinder(d = 2, h = h + 2 * eps, $fn = 12);
    translate([0, 0, -eps]) cylinder(d = qtr_clear, h = h + 2 * eps, $fn = 32);
    translate([0, 0, h - qtr_nut_h]) hex(qtr_nut_af, qtr_nut_h + eps);
  }
}

// ---------------------------------------------------------------- output
module assembly() {
  color("SteelBlue") base();
  color("Gainsboro") translate([0, 0, platter_z]) platter();
  color("SlateGray") translate([0, 0, recess_h - fit - retainer_t]) retainer();
  color("Orange") translate([0, 0, platter_z + platter_h - mast_pocket]) mast();
}

if (part == "base") base();
else if (part == "platter") platter();
else if (part == "retainer") retainer();
else if (part == "mast") mast();
else if (part == "lock_wheel") lock_wheel();
else assembly();
