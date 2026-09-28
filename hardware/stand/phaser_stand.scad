// Rotating azimuth stand for the CN0566 Phaser.
//
// Manual turntable with 5-degree detents and an engraved scale, built so a
// stepper can be added later without reprinting the base (GT2 teeth are
// molded into the platter rim, and the base has two inserts for a bracket).
//
// Frame: Z up, rotation axis = Z. +Y is boresight: the patch face looks
// toward +Y when the pointer reads 0. The mast is offset so the array phase
// center sits on the rotation axis.
//
// Render one part at a time:
//   openscad -D 'part="base"' -o base.stl phaser_stand.scad
// Parts: base, lid, platter, mast, lock_wheel, assembly (preview only).

part = "assembly";

// ---------------------------------------------------------------- measured
// Measured on the kit, looking at the patch face, bottom edge down.
board_w          = 113.19; // PCB width
thread_from_left = 52.28;  // 1/4-20 thread axis from left PCB edge
// VERIFY: assumed centered on the PCB (8 x 14 mm pitch, config.py `d`).
// Measure left edge -> center of leftmost patch column and add 49.0.
array_center_from_left = board_w / 2;
thread_behind_board = 6.0; // thread axis behind the PCB plane
board_t             = 1.6;

// Thread axis position relative to the rotation axis. Viewer faces -Y, so
// the viewer's left is +X.
mast_x = array_center_from_left - thread_from_left;
mast_y = -(thread_behind_board + board_t / 2);

// ---------------------------------------------------------------- hardware
bb_d        = 6.0;    // airsoft BBs for the thrust race
bb_gap      = 1.2;    // base top to platter bottom
race_r      = 75;
b608_od     = 22.15;  // 608 skate bearing, press fit
b608_h      = 7;
b608_lip    = 2;      // retaining lip above the bearing
m8_clear    = 8.4;
m8_head_d   = 15.5;   // clears a 13 mm AF hex head
m8_head_h   = 6;
plunger_tap = 6.8;    // M8 ball spring plunger, tapped into plastic
plunger_r   = 60;
detent_step = 5;      // degrees
qtr_head_af = 11.3;   // 1/4-20 hex head, 7/16" AF + clearance
qtr_head_h  = 4.6;
qtr_clear   = 6.8;
qtr_nut_af  = 11.3;
qtr_nut_h   = 5.8;
m4_tap      = 3.3;
m3_tap      = 2.8;
insert_d    = 5.6;    // M4 heat-set insert
insert_h    = 6;

// ---------------------------------------------------------------- geometry
base_d      = 220;
base_h      = 26;
base_skin   = 6;
base_wall   = 3;
lid_t       = 3;

gt2_teeth   = 300;    // 300T rim; with a 20T pulley that is 15:1
platter_r   = gt2_teeth * 2 / (2 * PI) - 0.254;
platter_h   = 14;
belt_band   = 9;      // lower rim height carrying the teeth

mast_foot_d = 64;
mast_foot_t = 6;
mast_pocket = 3;
mast_h      = 80;     // platter top to stud face
mast_top    = 22;     // square pad; keep under the Phaser's mount block
mast_skin   = 4;

scale_r0    = platter_r + 2;
scale_r1    = base_d / 2 - 1.5;
engrave     = 0.6;

$fn = 96;
eps = 0.01;

// ---------------------------------------------------------------- helpers
module hex(af, h) { cylinder(d = af / cos(30), h = h, $fn = 6); }

module race_groove(z) {
  rotate_extrude($fn = 180)
    translate([race_r, z]) circle(d = bb_d + 0.3, $fn = 32);
}

// ---------------------------------------------------------------- base
module scale_marks() {
  for (a = [0 : 1 : 359]) {
    // Leave the rear clear for the motor-bracket inserts.
    if (abs(a - 180) > 10) {
      len = (a % 10 == 0) ? 6 : (a % 5 == 0) ? 4 : 2.2;
      w   = (a % 5 == 0) ? 0.8 : 0.5;
      rotate(-a) translate([-w / 2, scale_r0, base_h - engrave])
        cube([w, len, engrave + eps]);
    }
  }
  // Signed labels: 0 at boresight, positive clockwise seen from above.
  for (a = [-150 : 30 : 150]) {
    rotate(-a) translate([0, scale_r0 + 8.2, base_h - engrave])
      linear_extrude(engrave + eps)
        text(str(a), size = 3.6, halign = "center", valign = "bottom",
             font = "DejaVu Sans:style=Bold");
  }
}

lid_screw_r = 88;
lid_screw_a = [45, 135, 225, 315];
motor_insert_r = scale_r0 + 5;
motor_insert_a = [180 - 7, 180 + 7];

module base() {
  difference() {
    union() {
      // Shell: top skin + outer wall, open underneath for ballast.
      difference() {
        cylinder(d = base_d, h = base_h);
        translate([0, 0, -eps])
          cylinder(d = base_d - 2 * base_wall, h = base_h - base_skin + eps);
      }
      // Center boss for the 608.
      translate([0, 0, lid_t]) cylinder(d = 36, h = base_h - lid_t);
      // Detent plunger boss.
      translate([plunger_r, 0, lid_t]) cylinder(d = 16, h = base_h - lid_t);
      // Lid screw bosses.
      for (a = lid_screw_a) rotate(a) translate([lid_screw_r, 0, lid_t])
        cylinder(d = 10, h = base_h - lid_t);
      // Motor-bracket insert bosses.
      for (a = motor_insert_a) rotate(-a) translate([0, motor_insert_r, lid_t])
        cylinder(d = 11, h = base_h - lid_t);
    }
    // Lid recess.
    translate([0, 0, -eps]) cylinder(d = base_d - 2 * base_wall + eps, h = lid_t + eps);
    // 608 inserted from below, stops against the top lip.
    translate([0, 0, -eps]) cylinder(d = b608_od, h = base_h - b608_lip + eps);
    translate([0, 0, base_h - b608_lip - eps]) cylinder(d = 19.5, h = b608_lip + 2 * eps);
    // Plunger tapped hole, adjustable from below through the lid.
    translate([plunger_r, 0, -eps]) cylinder(d = plunger_tap, h = base_h + 2 * eps, $fn = 32);
    for (a = lid_screw_a) rotate(a) translate([lid_screw_r, 0, lid_t - eps])
      cylinder(d = m3_tap, h = 12, $fn = 24);
    for (a = motor_insert_a) rotate(-a) translate([0, motor_insert_r, base_h - insert_h])
      cylinder(d = insert_d, h = insert_h + eps, $fn = 24);
    race_groove(base_h + bb_gap / 2);
    scale_marks();
  }
}

module lid() {
  d = base_d - 2 * base_wall - 0.4;
  difference() {
    cylinder(d = d, h = lid_t);
    // Socket access to the M8 nyloc.
    translate([0, 0, -eps]) cylinder(d = 20, h = lid_t + 2 * eps);
    // Plunger adjustment access.
    translate([plunger_r, 0, -eps]) cylinder(d = 10, h = lid_t + 2 * eps);
    for (a = lid_screw_a) rotate(a) translate([lid_screw_r, 0, -eps]) {
      cylinder(d = 3.4, h = lid_t + 2 * eps, $fn = 24);
      cylinder(d1 = 6.5, d2 = 3.4, h = 1.8, $fn = 24);
    }
    // Recesses for 12 mm stick-on rubber feet.
    for (a = [0, 90, 180, 270]) rotate(a) translate([lid_screw_r, 0, -eps])
      cylinder(d = 12.5, h = 0.8 + eps);
  }
}

// ---------------------------------------------------------------- platter
module gt2_rim_2d() {
  // Approximate GT2 groove: 0.76 mm deep round-bottom notch per tooth.
  difference() {
    circle(r = platter_r, $fn = gt2_teeth * 2);
    for (i = [0 : gt2_teeth - 1]) rotate(i * 360 / gt2_teeth)
      translate([platter_r + 0.2, 0]) circle(d = 1.3, $fn = 10);
  }
}

mast_screw_r = 26;
mast_screw_a = [90, 210, 330];

module mast_foot_2d(d) {
  // D-shape: the flat at -Y keys the mast's orientation.
  intersection() {
    circle(d = d);
    translate([-d, -d / 2 + 4]) square([2 * d, 2 * d]);
  }
}

module platter() {
  hub_h = bb_gap + b608_lip - 0.2; // short, so tightening preloads the race
  difference() {
    union() {
      linear_extrude(belt_band) gt2_rim_2d();
      translate([0, 0, belt_band]) cylinder(r = platter_r, h = platter_h - belt_band, $fn = 240);
      // Hub that bears on the 608 inner race.
      translate([0, 0, -hub_h]) cylinder(d = 11.8, h = hub_h + eps);
      // Pointer.
      translate([0, 0, platter_h - 3]) linear_extrude(3)
        polygon([[-5, platter_r - 6], [5, platter_r - 6], [0.6, scale_r0 + 5.5], [-0.6, scale_r0 + 5.5]]);
    }
    // Center bolt and its counterbore under the mast foot.
    translate([0, 0, -hub_h - eps]) cylinder(d = m8_clear, h = platter_h + hub_h + 2 * eps, $fn = 32);
    translate([0, 0, platter_h - mast_pocket - m8_head_h])
      cylinder(d = m8_head_d, h = m8_head_h + eps, $fn = 48);
    // Mast pocket.
    translate([0, 0, platter_h - mast_pocket]) linear_extrude(mast_pocket + eps)
      offset(0.2) mast_foot_2d(mast_foot_d);
    for (a = mast_screw_a) rotate(a) translate([mast_screw_r, 0, platter_h - mast_pocket - 10])
      cylinder(d = m4_tap, h = 10 + eps, $fn = 24);
    race_groove(-bb_gap / 2);
    // Detent notches, one every detent_step degrees.
    for (a = [0 : detent_step : 359]) rotate(a) translate([0, plunger_r, 0])
      rotate([90, 0, 0]) cylinder(d = 3, h = 7, center = true, $fn = 4);
    // Belt anchor slot at the rear: fold both belt ends in and pin them.
    translate([-1.5, -platter_r - eps, -eps]) cube([3, 10, belt_band + eps]);
    translate([-6, -platter_r + 7, belt_band / 2]) rotate([0, 90, 0]) cylinder(d = 3.2, h = 12, $fn = 20);
    // Lightening pockets between race and mast.
    for (a = [30 : 60 : 359]) rotate(a) translate([0, 46, platter_h - 7])
      cylinder(d = 18, h = 7 + eps);
  }
}

// ---------------------------------------------------------------- mast
module mast() {
  top_z = mast_foot_t + mast_h - mast_pocket;
  difference() {
    union() {
      linear_extrude(mast_foot_t) mast_foot_2d(mast_foot_d);
      // Flared column rising to the thread position.
      hull() {
        translate([mast_x, mast_y, mast_foot_t - eps]) linear_extrude(eps)
          square(mast_top + 10, center = true);
        translate([mast_x, mast_y, mast_foot_t + 20]) linear_extrude(eps)
          square(mast_top, center = true);
      }
      translate([mast_x, mast_y, mast_foot_t]) linear_extrude(top_z - mast_foot_t)
        offset(r = 2) square(mast_top - 4, center = true);
    }
    for (a = mast_screw_a) rotate(a) translate([mast_screw_r, 0, -eps]) {
      cylinder(d = 4.4, h = mast_foot_t + 2 * eps, $fn = 24);
      translate([0, 0, mast_foot_t - 3]) cylinder(d = 8, h = 60, $fn = 32);
    }
    translate([mast_x, mast_y, 0]) {
      // Stud: 1/4-20 x 5/8" hex bolt, head slid in from the rear.
      translate([0, 0, top_z - mast_skin - eps]) cylinder(d = qtr_clear, h = mast_skin + 2 * eps, $fn = 32);
      translate([0, 0, top_z - mast_skin - qtr_head_h]) {
        hex(qtr_head_af, qtr_head_h);
        translate([-qtr_head_af / 2, -mast_top, 0]) cube([qtr_head_af, mast_top, qtr_head_h]);
      }
    }
    // Alignment line on the pad, parallel to the patch face.
    translate([mast_x - mast_top / 2, mast_y + mast_top / 2 - 2.5, top_z - 0.5])
      cube([mast_top, 0.6, 0.5 + eps]);
  }
}

// Knurled jam wheel: captive 1/4-20 nut, locks the Phaser's rotation on the stud.
module lock_wheel() {
  d = 26; h = 8;
  difference() {
    cylinder(d = d, h = h);
    for (i = [0 : 29]) rotate(i * 12) translate([d / 2 + 0.3, 0, -eps])
      cylinder(d = 2, h = h + 2 * eps, $fn = 12);
    translate([0, 0, -eps]) cylinder(d = qtr_clear, h = h + 2 * eps, $fn = 32);
    translate([0, 0, h - qtr_nut_h]) hex(qtr_nut_af, qtr_nut_h + eps);
  }
}

// ---------------------------------------------------------------- output
module assembly() {
  color("SlateGray") lid();
  color("SteelBlue") base();
  color("Gainsboro") translate([0, 0, base_h + bb_gap]) platter();
  color("Orange") translate([0, 0, base_h + bb_gap + platter_h - mast_pocket]) mast();
}

if (part == "base") base();
else if (part == "lid") lid();
else if (part == "platter") platter();
else if (part == "mast") mast();
else if (part == "lock_wheel") lock_wheel();
else assembly();
